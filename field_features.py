"""Field services: durable upload outbox, Wi-Fi UI, searchable recovery history."""
import collections
from contextlib import closing
import csv
from datetime import datetime,timedelta,timezone
import gzip
import hashlib
import html
import io
import json
import logging
import math
from pathlib import Path
import re
import socket
import shutil
import threading
import time
import xml.etree.ElementTree as ET

import requests
from dateutil.parser import isoparse
from flask import Response,abort,jsonify,request,render_template_string

M=None
settings_lock=threading.RLock()
net_lock=threading.Lock()
DEFAULT=dict(enabled=False,callsign='N0CALL',aprs_server='noam.aprs2.net',aprs_port=14580,
             aprs_max_age_seconds=120,sondehub_max_age_seconds=180,dev_uploads=True)
SETTINGS={}
ENDPOINTS={'amateur':'https://api.v2.sondehub.org/amateur/telemetry'}
network_state=dict(status='Starting',last_success=None,last_error='',last_destination='',
                   delivery_note='APRS socket writes are not end-to-end acknowledgments.')
# Reserved test IDs never become live SondeHub telemetry.
TESTNAME=re.compile(r'(?:4FSKTEST|PISONDE[-_]?TEST)',re.I)


def utc():return datetime.now(timezone.utc)
def stamp(d):return d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
def load_settings():
    global SETTINGS
    SETTINGS=dict(DEFAULT)
    path=M.ROOT/'uploads.json'
    if path.exists():SETTINGS.update(json.loads(path.read_text()))
    validate_settings(SETTINGS)

def validate_settings(s):
    if not re.fullmatch(r'[A-Z0-9]{1,6}(?:-\d{1,2})?',str(s['callsign'])):
        raise ValueError('Use an amateur callsign with optional SSID')
    if s['enabled'] and str(s['callsign']).split('-')[0] in ('N0CALL','NOCALL'):
        raise ValueError('Set your own callsign before enabling uploads')
    if not isinstance(s['enabled'],bool) or not isinstance(s['dev_uploads'],bool):raise ValueError('Invalid upload switch')

def metadata(settings,received):
    return dict(software_name='PiSonde-Mobile',software_version=M.VERSION,
                uploader_callsign=settings['callsign'],time_received=stamp(received),
                uploader_radio='RTL-SDR Blog V4',uploader_antenna='2m/70cm mobile antenna')

def parse_datetime(value,received):
    if not isinstance(value,str):raise ValueError('Missing payload time')
    if re.fullmatch(r'\d\d:\d\d:\d\d(?:\.\d+)?',value):
        times=[isoparse((received+timedelta(days=day)).date().isoformat()+'T'+value+'Z') for day in (-1,0,1)]
        return min(times,key=lambda d:abs((received-d).total_seconds()))
    d=isoparse(value)
    if d.tzinfo is None:raise ValueError('Payload time must identify UTC')
    return d.astimezone(timezone.utc)

def numeric(value):return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def aprs_gate(raw,callsign):
    if not isinstance(raw,str) or any(c in raw for c in '\r\n\0') or ':' not in raw or '>' not in raw:
        return None
    header,info=raw.split(':',1)
    route=header.split('>',1)[1].split(',')
    upper=[x.rstrip('*').upper() for x in route[1:]]
    if any(x in ('TCPIP','TCPXX','NOGATE','RFONLY') or x.startswith('QA') for x in upper):return None
    if info.startswith('?'):return None
    if info.startswith('}'):
        # Conservative third-party handling: never regate encapsulated internet traffic.
        return None
    return header+',qAO,'+callsign+':'+info

def format_sonde(row,settings):
    raw=json.loads(row['raw_packet'])
    received=isoparse(row['received_utc']).astimezone(timezone.utc)
    out=metadata(settings,received)
    mode=row['protocol']
    if mode=='HORUS':
        from horusdemodlib.utils import telem_to_sondehub
        test=bool(TESTNAME.search(str(raw.get('callsign',''))))
        out=telem_to_sondehub(raw,metadata=out,check_time=not test)
        if not out:raise ValueError('Horus packet not eligible for SondeHub')
        out['frequency']=row['frequency_mhz']
        if test:
            if not settings['dev_uploads']:raise ValueError('Development uploads disabled')
            out['dev']='PiSonde Mobile synthetic RF/decoder test; do not publish'
            # A reusable reserved-ID audio fixture has an intentionally fixed GPS clock.
            # Use receipt time only for its DEV validation; keep decoded raw time locally.
            out['datetime']=stamp(received)
        return 'amateur',out,test
    raise ValueError('Only Horus telemetry supports direct HTTP uploads; weather reception is local-only')


def create_schema():
    with M.database() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS upload_jobs(
          id INTEGER PRIMARY KEY,reception_id INTEGER UNIQUE,
          destination TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
          created_utc TEXT NOT NULL,next_attempt REAL NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '',
          completed_utc TEXT);
        CREATE INDEX IF NOT EXISTS upload_jobs_pending ON upload_jobs(status,next_attempt,id);
        CREATE TABLE IF NOT EXISTS target_notes(
          protocol TEXT NOT NULL,callsign TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',
          recovery_status TEXT NOT NULL DEFAULT 'Watching',PRIMARY KEY(protocol,callsign));
        CREATE INDEX IF NOT EXISTS receptions_target ON receptions(protocol,callsign,id);
        ''')
    retire_weather_jobs()

def retire_weather_jobs():
    with M.database() as db:
        db.execute("""UPDATE upload_jobs SET status='disabled',completed_utc=?,
          last_error='Weather uploads removed in 0.3.19; local history retained'
          WHERE destination='sondehub' AND status='pending'""", (stamp(utc()),))

def add_job(row):
    with settings_lock:s=dict(SETTINGS)
    if not s['enabled'] or row['protocol'] not in ('APRS','HORUS'):return
    received=isoparse(row['received_utc']).astimezone(timezone.utc)
    try:
        if row['protocol']=='APRS':
            payload=aprs_gate(row['raw_packet'],s['callsign'])
            if payload is None:return
            destination='aprs'
        else:
            destination,payload,test=format_sonde(row,s)
            payload=json.dumps(payload,separators=(',',':'),allow_nan=False)
        with M.database() as db:
            db.execute('''INSERT OR IGNORE INTO upload_jobs
              (reception_id,destination,payload,created_utc) VALUES (?,?,?,?)''',
              (row['id'],destination,payload,stamp(received)))
    except Exception as exc:
        logging.info('Kept locally; upload withheld for %s: %s',row['callsign'],exc)


def settings_snapshot():
    with settings_lock:return dict(SETTINGS)

def complete(ids,status,error=''):
    with M.database() as db:
        db.executemany('UPDATE upload_jobs SET status=?,last_error=?,completed_utc=? WHERE id=?',
                       [(status,error,stamp(utc()),ident) for ident in ids])


def send_aprs(lines,s):
    # APRS-IS passcode is the standard callsign hash; do not expose it in UI/logs.
    base=s['callsign'].split('-')[0].upper();code=0x73e2
    for i,ch in enumerate(base):code^=ord(ch)<<(8 if i%2==0 else 0)
    code &= 0x7fff
    with socket.create_connection((s['aprs_server'],s['aprs_port']),timeout=8) as sock:
        sock.settimeout(8)
        sock.sendall(f"user {s['callsign']} pass {code} vers PiSondeMobile {M.VERSION} filter m/0\r\n".encode())
        buffer=b'';verified=False;deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            data=sock.recv(4096)
            if not data:raise RuntimeError('APRS-IS disconnected before login verification')
            buffer+=data
            if b' unverified' in buffer.lower():raise RuntimeError('APRS-IS login was not verified')
            if b' verified' in buffer.lower():verified=True;break
        if not verified:raise RuntimeError('No verified APRS-IS login response')
        for line in lines:sock.sendall(line.encode('latin-1',errors='replace')+b'\r\n')


def validate_sondehub_response(response,payload):
    if response.status_code not in (200,201,202):
        raise RuntimeError('HTTP '+str(response.status_code)+': '+response.text[:300])
    try:reply=response.json()
    except Exception:raise RuntimeError('SondeHub response was not JSON; delivery unconfirmed')
    if not isinstance(reply,dict) or not isinstance(reply.get('errors',[]),list):
        raise RuntimeError('Unrecognized SondeHub response; delivery unconfirmed')
    errors=reply.get('errors',[])
    development=bool(payload) and all(p.get('dev') for p in payload)
    expected='All checks passed however payload contained dev flag so will not be uploaded to the database'
    if errors:
        dev_pass=development and all(isinstance(item,dict) and item.get('error_message')==expected for item in errors)
        if not dev_pass:raise RuntimeError('SondeHub validation: '+json.dumps(errors)[:1500])
    elif response.status_code==202:
        # SondeHub uses 202 for partially processed batches; never infer acceptance
        # when a partial response omits the per-payload errors.
        raise RuntimeError('SondeHub partial response without validation details; delivery unconfirmed')
    elif not isinstance(reply.get('message'),str):
        raise RuntimeError('SondeHub response missing acknowledgment; delivery unconfirmed')
    return 'test_passed' if development else 'accepted'


def upload_cycle(http=requests):
    retire_weather_jobs()
    s=settings_snapshot()
    if not s['enabled']:
        network_state['status']='Paused';return
    now=utc()
    with M.database() as db:
        # Expire the entire old backlog before selecting live jobs: a long offline period
        # must not delay today's receptions behind thousands of stale retry entries.
        db.execute("""UPDATE upload_jobs SET status='expired',completed_utc=?,
          last_error='Outside live upload age window; local history retained'
          WHERE status='pending' AND ((destination='aprs' AND created_utc<?)
            OR (destination!='aprs' AND created_utc<?) OR created_utc>?)""",
          (stamp(now),stamp(now-timedelta(seconds=s['aprs_max_age_seconds'])),
           stamp(now-timedelta(seconds=s['sondehub_max_age_seconds'])),stamp(now+timedelta(seconds=30))))
        rows=db.execute("SELECT * FROM upload_jobs WHERE status='pending' AND destination IN ('aprs','amateur') AND next_attempt<=? ORDER BY id LIMIT 100",(time.time(),)).fetchall()
    if not rows:
        network_state['status']='Waiting for eligible packets';return
    groups=collections.defaultdict(list)
    for row in rows:
        age=(utc()-isoparse(row['created_utc'])).total_seconds()
        limit=s['aprs_max_age_seconds'] if row['destination']=='aprs' else s['sondehub_max_age_seconds']
        if age>limit or age< -30:
            complete([row['id']],'expired','Outside live upload age window; local history retained')
        else:groups[row['destination']].append(row)
    for destination,items in groups.items():
        ids=[x['id'] for x in items]
        try:
            if destination=='aprs':
                send_aprs([x['payload'] for x in items],s)
                result='sent'
            elif destination=='amateur':
                payload=[json.loads(x['payload']) for x in items]
                body=gzip.compress(json.dumps(payload,allow_nan=False).encode())
                response=http.put(ENDPOINTS[destination],data=body,headers={
                    'Content-Type':'application/json','Content-Encoding':'gzip',
                    'User-Agent':'PiSonde-Mobile/'+M.VERSION},timeout=(8,10))
                if response.status_code not in (200,201,202):
                    raise RuntimeError('HTTP '+str(response.status_code)+': '+response.text[:300])
                result=validate_sondehub_response(response,payload)
            else:
                raise RuntimeError('Unsupported upload destination')
            complete(ids,result)
            network_state.update(status=('Development test validated: ' if result=='test_passed' else 'Sent to ')+destination,last_success=stamp(utc()),
                                 last_error='',last_destination=destination)
            logging.info('Upload %s: %d packets %s',destination,len(ids),result)
        except Exception as exc:
            error=str(exc)[:500]
            network_state.update(status='Retrying '+destination,last_error=error)
            with M.database() as db:
                for item in items:
                    delay=min(60,5*2**min(item['attempts'],4))
                    db.execute('UPDATE upload_jobs SET attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?',
                               (time.time()+delay,error,item['id']))
            logging.warning('Upload retry %s: %s',destination,error)


def uploader():
    while not M.shutdown_event.wait(30):
        try:upload_cycle()
        except Exception:logging.exception('Upload worker error')


def broker(action,**values):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(40);sock.connect('/run/pisonde-control/control.sock')
        sock.sendall(json.dumps(dict(action=action,**values)).encode()+b'\n')
        with sock.makefile('rb') as stream:raw=stream.readline(100000)
    answer=json.loads(raw)
    if not answer.get('ok'):raise ValueError(answer.get('error','Wi-Fi operation failed'))
    return answer['result']


def wifi_watch():
    while not M.shutdown_event.wait(60):
        try:broker('ensure')
        except Exception as exc:logging.warning('Wi-Fi watchdog: %s',exc)


def upload_status():
    with M.database() as db:
        counts={row[0]:row[1] for row in db.execute('SELECT status,COUNT(*) FROM upload_jobs GROUP BY status')}
        destinations={}
        for dest in ('aprs','amateur','sondehub'):
            grouped={row[0]:row[1] for row in db.execute('SELECT status,COUNT(*) FROM upload_jobs WHERE destination=? GROUP BY status',(dest,))}
            last=db.execute('SELECT status,last_error,created_utc,completed_utc FROM upload_jobs WHERE destination=? ORDER BY id DESC LIMIT 1',(dest,)).fetchone()
            destinations[dest]=dict(counts=grouped,last=dict(last) if last else None)
        samples={row[0]:row[1] for row in db.execute('''SELECT r.protocol,COUNT(*) FROM upload_jobs j
            JOIN receptions r ON r.id=j.reception_id WHERE j.destination='sondehub'
            GROUP BY r.protocol''')}
    return dict(network_state,counts=counts,settings=settings_snapshot(),destinations=destinations,
                weather_validation_samples={mode:samples.get(mode,0) for mode in ('DFM','RS41')})


def weather_validation():
    mode=request.args.get('type','').upper()
    if mode not in ('DFM','RS41'):abort(400)
    def generate():
        with M.database() as db:
            # Export the exact immutable payload queued at reception time, even if
            # now expired or rejected. Never re-date or resend these samples.
            for row in db.execute('''SELECT j.payload FROM upload_jobs j
                 JOIN receptions r ON r.id=j.reception_id
                 WHERE j.destination='sondehub' AND r.protocol=? ORDER BY j.id''',(mode,)):
                yield row['payload']+'\n'
    return Response(generate(),mimetype='application/x-ndjson',headers={
        'Content-Disposition':f'attachment; filename="PiSonde-Mobile-{mode}-validation.jsonl"',
        'Cache-Control':'no-store'})


def upload_report():
    return Response(json.dumps(upload_status(),indent=2),mimetype='application/json',headers={
        'Content-Disposition':'attachment; filename="PiSonde-Mobile-upload-report.json"',
        'Cache-Control':'no-store'})


RETENTION_DEFAULT=dict(mode='forever',days=90,threshold=10,protect_latest=True)
retention_lock=threading.RLock()
retention_wake=threading.Event()
retention_state={}

def validate_retention(value):
    if not isinstance(value,dict) or set(value)!=set(RETENTION_DEFAULT):raise ValueError('Invalid retention settings')
    if value['mode'] not in ('forever','days','low_space'):raise ValueError('Choose a retention mode')
    if type(value['days']) is not int or value['days'] not in (30,60,90,180,365):raise ValueError('Choose a listed retention period')
    if type(value['threshold']) is not int or value['threshold'] not in (5,10,15,20):raise ValueError('Choose a listed free-space threshold')
    if type(value['protect_latest']) is not bool:raise ValueError('Invalid location protection option')
    return value

def retention_policy():
    path=M.ROOT/'retention.json'
    return validate_retention(json.loads(path.read_text()) if path.exists() else dict(RETENTION_DEFAULT))

def free_percent():
    usage=shutil.disk_usage(M.ROOT)
    return round(100*usage.free/usage.total,2)

def retention_status():
    with retention_lock:
        try:cfg=retention_policy();error=''
        except Exception:cfg=dict(RETENTION_DEFAULT);error='Invalid retention settings; automatic deletion stopped'
        return dict(retention_state,policy=cfg,free_percent=free_percent(),settings_error=error)

def retention_api():
    if request.method=='GET':return jsonify(retention_status())
    M.csrf();value=request.get_json(silent=True)
    if not isinstance(value,dict) or set(value)!={'policy','confirmed'}:return jsonify(error='Invalid retention request'),400
    try:cfg=validate_retention(value['policy'])
    except ValueError as exc:return jsonify(error=str(exc)),400
    if type(value['confirmed']) is not bool or cfg['mode']!='forever' and not value['confirmed']:
        return jsonify(error='Confirm permanent history deletion before enabling retention'),400
    with retention_lock:
        M.atomic_json(M.ROOT/'retention.json',cfg)
        retention_state.update(last_error='',message='Retention policy saved; awaiting background check')
    retention_wake.set()
    return jsonify(ok=True,**retention_status())

def retention_candidates(db,cfg,cutoff):
    # Retain the highest row ID so INTEGER PRIMARY KEY never reuses deleted IDs.
    sql='''SELECT r.id FROM receptions r WHERE r.id<(SELECT MAX(id) FROM receptions)
           AND julianday(r.received_utc)<julianday(?)'''
    if cfg['protect_latest']:
        sql+=''' AND r.id NOT IN (
          SELECT MAX(id) FROM receptions WHERE latitude IS NOT NULL AND longitude IS NOT NULL GROUP BY protocol,callsign
          UNION SELECT MAX(p.id) FROM receptions p JOIN target_notes n
            ON n.protocol=p.protocol AND n.callsign=p.callsign
            WHERE trim(n.note)<>'' OR n.recovery_status<>'Watching' GROUP BY p.protocol,p.callsign)'''
    return [row[0] for row in db.execute(sql+' ORDER BY r.id LIMIT 500',(cutoff,))]

def compact_history():
    # VACUUM needs working space. Never delete other files to make it fit.
    if shutil.disk_usage(M.ROOT).free<M.DB.stat().st_size*2+64*1024*1024:
        return 'Not enough working space to compact SQLite safely; free other storage first'
    deadline=time.monotonic()+5
    with M.database() as db:
        db.execute('PRAGMA busy_timeout=500')
        db.set_progress_handler(lambda: int(time.monotonic()>deadline),5000)
        try:
            db.execute('VACUUM')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:db.set_progress_handler(None,0)
    return ''

def retention_cycle():
    with retention_lock:
        cfg=retention_policy();deleted=0;error='';message='Keeping all history'
        before=free_percent()
        if cfg['mode']=='forever':return
        if cfg['mode']=='low_space' and before>=cfg['threshold']:
            message='Free space is above the cleanup threshold'
        else:
            if cfg['mode']=='low_space' and shutil.disk_usage(M.ROOT).free<M.DB.stat().st_size*2+64*1024*1024:
                message='No history deleted: insufficient SQLite compaction working space'
                error='Free other storage first; SQLite needs temporary space to shrink safely'
            else:
                cutoff=stamp(utc()-timedelta(days=cfg['days'])) if cfg['mode']=='days' else stamp(utc()-timedelta(minutes=5))
                deadline=time.monotonic()+3
                # One bounded transaction; interruption rolls back the whole batch.
                with M.database() as db:
                    db.execute('PRAGMA busy_timeout=500')
                    db.set_progress_handler(lambda: int(time.monotonic()>deadline),5000)
                    try:
                        for _ in range(20):
                            ids=retention_candidates(db,cfg,cutoff)
                            if not ids:break
                            marks=','.join('?' for _ in ids)
                            db.execute('DELETE FROM upload_jobs WHERE reception_id IN ('+marks+')',ids)
                            db.execute('DELETE FROM receptions WHERE id IN ('+marks+')',ids)
                            deleted+=len(ids)
                            if len(ids)<500:break
                    finally:db.set_progress_handler(None,0)
                if deleted:
                    with M.lock:M.status['packets']=max(0,M.status['packets']-deleted)
                    callback=getattr(M,'retention_changed',None)
                    if callback:callback()
                    M.retention_generation=time.time_ns()
                    logging.info('Retention removed %s receptions and linked upload payloads',deleted)
                if cfg['mode']=='low_space':
                    with M.database() as db:needs_compaction=deleted or db.execute('PRAGMA freelist_count').fetchone()[0]>0
                    try:error=compact_history() if needs_compaction else ''
                    except Exception as exc:error='SQLite compaction deferred: '+str(exc)
                    message='Oldest eligible history cleaned; '+str(free_percent())+'% free'
                    if free_percent()<cfg['threshold'] and not error:
                        message+='; remaining history is protected/recent, cleanup is bounded, or other files use the space'
                else:message='Removed eligible history older than '+str(cfg['days'])+' days; freed SQLite pages are reused'
        retention_state.update(last_run=stamp(utc()),last_deleted=deleted,deleted_total=retention_state.get('deleted_total',0)+deleted,message=message,last_error=error)
        M.atomic_json(M.ROOT/'retention-status.json',retention_state)

def retention_worker():
    while not M.shutdown_event.is_set():
        try:retention_cycle()
        except Exception as exc:
            with retention_lock:retention_state.update(last_error=str(exc),message='Cleanup deferred; check retention status')
            logging.exception('Retention maintenance deferred')
        for _ in range(300):
            if M.shutdown_event.wait(1):return
            if retention_wake.is_set():retention_wake.clear();break

def power_api():
    M.csrf();value=request.get_json(silent=True)
    if not isinstance(value,dict) or set(value)!={'operation'} or value['operation'] not in ('restart','shutdown'):
        return jsonify(error='Choose restart or shutdown'),400
    try:return jsonify(broker('power',operation=value['operation']))
    except Exception as exc:return jsonify(error=str(exc)),409


def install(module,start_threads=True):
    global M
    M=module;load_settings();create_schema()
    M.retention_generation=0;retention_wake.clear();retention_state.clear()
    try:
        path=M.ROOT/'retention-status.json'
        if path.exists():retention_state.update(json.loads(path.read_text()))
    except Exception:logging.warning('Prior retention status could not be read')
    original_store=M.store_packet
    def store(packet,mode,hz):
        row=original_store(packet,mode,hz)
        try:add_job(row)
        except Exception:logging.exception('Local packet retained; could not enqueue upload')
        return row
    M.store_packet=store
    status_view=M.app.view_functions['api_status']
    def api_status():
        response=status_view();data=response.get_json()
        data['upload']=upload_status()
        data['uploads']=data['upload']['status']
        data['retention']=retention_status();data['retention_generation']=M.retention_generation
        data['storage_warning']='Low storage: '+str(data['retention']['free_percent'])+'% free; check Uploads retention settings.' if data['retention']['free_percent']<data['retention']['policy']['threshold'] else ''
        return jsonify(data)
    M.app.view_functions['api_status']=api_status
    M.app.view_functions['history']=history_csv
    M.app.add_url_rule('/wifi','field_wifi',wifi_page)
    M.app.add_url_rule('/api/wifi','field_wifi_api',wifi_api,methods=['POST'])
    M.app.add_url_rule('/history','field_history',history_page)
    M.app.add_url_rule('/api/targets','field_targets',targets)
    M.app.add_url_rule('/api/notes','field_notes',notes,methods=['POST'])
    M.app.add_url_rule('/track.gpx','field_gpx',gpx)
    M.app.add_url_rule('/uploads','field_uploads',uploads_page)
    M.app.add_url_rule('/weather-validation.jsonl','field_weather_validation',weather_validation)
    M.app.add_url_rule('/upload-report.json','field_upload_report',upload_report)
    M.app.add_url_rule('/api/retention','field_retention',retention_api,methods=['GET','POST'])
    M.app.add_url_rule('/device','field_device',device_page)
    M.app.add_url_rule('/api/power','field_power',power_api,methods=['POST'])
    M.app.add_url_rule('/api/uploads','field_uploads_api',upload_settings,methods=['POST'])
    M.app.add_url_rule('/api/upload-test','field_test',development_upload_test,methods=['POST'])
    M.app.add_url_rule('/api/test-horus','field_test_horus',test_horus_packet,methods=['POST'])
    M.PAGE=M.PAGE.replace('<p id="summary"></p>', '<p id="summary"></p><p id="upload-status"></p><p><a href="/wifi">Wi-Fi</a> · <a href="/history">Recovery history</a> · <a href="/uploads">Uploads</a></p>')
    M.PAGE=M.PAGE.replace('Uploads: not configured yet. Receiver location: not published.', 'Receiver location: not published.')
    M.PAGE=M.PAGE.replace("const fragment=document.createDocumentFragment();", "$('upload-status').textContent=s.upload.status+' · queued '+(s.upload.counts.pending||0)+' · SondeHub accepted '+(s.upload.counts.accepted||0)+' · APRS sent '+(s.upload.counts.sent||0)+' · DEV tests validated '+(s.upload.counts.test_passed||0)+(s.storage_warning?' · '+s.storage_warning:'');const fragment=document.createDocumentFragment();")
    if start_threads:
        threading.Thread(target=uploader,name='uploads',daemon=True).start()
        threading.Thread(target=retention_worker,name='retention',daemon=True).start()
        threading.Thread(target=wifi_watch,name='wifi-watchdog',daemon=True).start()


def token():
    from flask import session
    session.setdefault('token',__import__('secrets').token_hex(24));return session['token']

THEME='[hidden]{display:none!important}:root{--blue:#155eab;--green:#177443;--red:#b52b36;--amber:#8a5100;--ink:#183047;--line:#c6d4e2}body{color:var(--ink);background:#f6f9fc;margin:16px;line-height:1.5}h1,h2,h3{color:#143c62}button{background:var(--blue);color:white;border:1px solid var(--blue);border-radius:7px;min-height:44px;font-weight:600;padding:10px 14px;cursor:pointer}button:hover:not(:disabled){filter:brightness(.90)}button:disabled{opacity:.55;cursor:wait}button.success,#resume,#save,#retention-save,#save-policy{background:var(--green);border-color:var(--green);color:white}button.danger,#stop{background:var(--red);border-color:var(--red);color:white}button.warning,#keep{background:var(--amber);border-color:var(--amber);color:white}a{color:#125b9c}nav{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}nav a{display:inline-flex;align-items:center;min-height:44px;padding:8px 13px;border:1px solid #bed0e2;border-radius:7px;background:#e7f0fa;color:#174e80;text-decoration:none;font-weight:600}nav a.active,nav a[aria-current=page]{background:var(--blue);color:white;border-color:var(--blue)}nav a:hover{background:#d5e5f7;color:#143c62}input,select,textarea{border:1px solid #aebfd0;border-radius:6px;background:white;color:var(--ink);min-height:44px}input[type=checkbox]{min-height:auto;accent-color:var(--blue)}fieldset,.device-controls{border:1px solid var(--line);border-radius:8px;padding:12px;margin:16px 0;background:white}legend,summary{font-weight:600}.target{background:white;border-color:var(--line);border-left:4px solid #568cbb}.error{color:#a5232f}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:3px solid #bb6b00;outline-offset:3px}.device-controls button{margin-right:8px}label{display:block}pre{white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:480px){nav a{padding:8px 10px;font-size:.95rem}body{margin:12px}fieldset{padding:10px}}\n'
POWER_UI='<details class="device-controls"><summary>Device controls</summary><p>Restart or safely shut down this Pi. Reception and the webpage will stop during the operation.</p><button type="button" class="warning" data-power="restart">Restart Pi</button><button type="button" class="danger" data-power="shutdown">Shut down Pi</button><p id="power-result" role="status"></p></details>\n<script>\nfor(const button of document.querySelectorAll(\'[data-power]\'))button.onclick=async()=>{\n const operation=button.dataset.power,message=operation===\'restart\'?\'Restart this Pi now? Reception will pause and the webpage will disconnect.\':\'Shut down this Pi now? Wait for normal shutdown to finish before unplugging power.\';\n if(!confirm(message))return;\n const output=document.getElementById(\'power-result\'),buttons=document.querySelectorAll(\'[data-power]\');for(const b of buttons)b.disabled=true;\n output.textContent=\'Scheduling \'+operation+\'…\';const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),10000);\n try{const response=await fetch(\'/api/power\',{method:\'POST\',signal:abort.signal,headers:{\'Content-Type\':\'application/json\',\'X-CSRF-Token\':{{token|tojson}}},body:JSON.stringify({operation})});const value=await response.json();if(!response.ok)throw Error(value.error||\'Power action failed\');output.textContent=operation===\'restart\'?\'Restart scheduled. Reconnect to the Pi and refresh this page after startup.\':\'Shutdown scheduled. Wait for normal shutdown to finish before removing power.\';}\n catch(e){output.textContent=\'Could not confirm power action: \'+e.message+\'. Check the Pi before retrying.\';for(const b of buttons)b.disabled=false;}finally{clearTimeout(timer);}\n};\n</script>\n'

BASE='''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>PiSonde Mobile</title><style>
body{font:18px system-ui;margin:16px;max-width:850px}input,button,select,textarea{font:inherit;padding:10px;margin:5px 0;max-width:100%;box-sizing:border-box}input,select,textarea{width:100%}article{border-top:1px solid #aaa;padding:12px 0}small{overflow-wrap:anywhere}a{display:inline-block;padding:6px 0}.error{color:#a00}</style></head><body><a href="/">← Receiver</a> CONTENT </body></html>'''

def device_page():
    controls=POWER_UI.replace('<details class="device-controls"><summary>Device controls</summary>','<section class="device-controls">').replace('</details>','</section>')
    return page('<h2>Device controls</h2>'+controls)

def navigation():
    links=(('/','Receiver'),('/wifi','Wi-Fi'),('/history','History'),('/uploads','Uploads'),('/updates','Updates'),('/device','Device controls'),('/help','Help'))
    return '<nav aria-label="Main">'+''.join('<a href="'+url+'"'+(' aria-current="page"' if request.path==url else '')+'>'+label+'</a>' for url,label in links)+'</nav>'

def page(content,**data):
    template=BASE.replace('<a href="/">← Receiver</a>',navigation()).replace('</style>',THEME+'</style>').replace('CONTENT',content)
    return render_template_string(template,token=token(),**data)


def wifi_page():
    return page('''<h2>Wi-Fi hotspots</h2><p>The onboard radio keeps pisondemobile available. The Linksys USB radio joins Internet hotspots.</p>
<p id="state"></p><p id="adapters"></p><p id="wifi-recovery" role="status"></p><button onclick="load()">Refresh / scan</button><label>Nearby hotspots<select id="nearby"><option value="">Select or type below</option></select></label>
<label>Hotspot name<input id="ssid" maxlength="32" autocomplete="off"></label><label>Password<input id="password" type="password" autocomplete="new-password"></label><button id="connect">Connect and save</button>
<h3>Saved hotspots</h3><div id="saved"></div><p id="message"></p><script>
const token={{token|tojson}},$=id=>document.getElementById(id);async function call(v){const r=await fetch('/api/wifi',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(v)});const d=await r.json();if(!r.ok)throw Error(d.error||'Wi-Fi request failed');return d;}
function showWifiHealth(s){const health=s.recovery||{};$('wifi-recovery').textContent='Wi-Fi recovery: '+(health.state||'unavailable')+' · Adapter attempts: '+(health.adapter_attempts||0)+' · Shared service attempts: '+(health.service_attempts||0)+(health.last_reason?' · '+health.last_reason:'')+(health.last_error?' · '+health.last_error:'');}
setInterval(async()=>{try{const response=await fetch('/api/status',{cache:'no-store'});const value=await response.json();if(value.ui_wifi&&value.ui_wifi.recovery)showWifiHealth(value.ui_wifi);}catch(e){}},15000);
async function load(confirmation='',rescan=true){try{
$('message').textContent=rescan?'Scanning…':'Updating connection status…';
const s=await call({action:'status'});
$('adapters').textContent='Onboard AP MAC: '+s.ap_mac+' ('+(s.ap_interface||'missing')+') · Linksys upstream MAC: '+s.client_mac+' ('+(s.client_interface||'unplugged')+')';$('state').textContent='Upload connection: '+(s.hotspot||'offline')+' · Onboard AP: '+(s.ap_present?'present':'missing');
showWifiHealth(s);
const saved=await call({action:'saved'});$('saved').replaceChildren();
for(const v of saved){
 const row=document.createElement('div'),connect=document.createElement('button'),forget=document.createElement('button');
 connect.textContent=v.ssid;forget.textContent='Forget';forget.style.marginLeft='0.5em';
 connect.onclick=async()=>{try{$('message').textContent='Connecting…';await call({action:'saved_connect',uuid:v.uuid});await load('Connected; preferred network: '+v.ssid,false);}catch(e){$('message').textContent=e.message;}};
 forget.onclick=async()=>{
  if(!confirm('Forget all saved profiles for "'+v.ssid+'"? Uploads may go offline. The pisondemobile dashboard stays available.'))return;
  forget.disabled=true;connect.disabled=true;
  try{const r=await call({action:'forget',uuid:v.uuid});await load('Forgot '+r.forgotten+' ('+r.removed_profiles+' saved profile(s)).',false);}
  catch(e){$('message').textContent=e.message;forget.disabled=false;connect.disabled=false;}
 };
 row.append(connect,forget);$('saved').append(row);
}
if(rescan&&s.usb_present){const list=await call({action:'scan'});$('nearby').replaceChildren(new Option('Select or type below',''),...list.map(v=>new Option(v.ssid+' · '+v.signal+'%',v.ssid)));}
$('message').textContent=confirmation||(!s.usb_present?'Linksys unplugged. Saved networks can still be forgotten.':'');
}catch(e){$('message').textContent=(confirmation?confirmation+' · ':'')+e.message;}}
$('nearby').onchange=()=>$('ssid').value=$('nearby').value;$('connect').onclick=async()=>{$('connect').disabled=true;try{$('message').textContent='Connecting…';await call({action:'connect',ssid:$('ssid').value,password:$('password').value});$('password').value='';await load('Hotspot connected and saved.',false);}catch(e){$('message').textContent=e.message;}finally{$('connect').disabled=false;}};load();</script>''')


def wifi_api():
    M.csrf();value=request.get_json(silent=True)
    if not isinstance(value,dict) or value.get('action') not in ('status','scan','saved','connect','saved_connect','forget'):abort(400)
    try:return jsonify(broker(**value))
    except Exception as exc:return jsonify(error=str(exc)),400


def targets():
    query=request.args.get('query','')[:100]
    with M.database() as db:
        rows=db.execute('''SELECT r.protocol,r.callsign,MIN(r.received_utc) first_seen,
          MAX(r.received_utc) last_seen,COUNT(*) packets,
          COALESCE(n.note,'') note,COALESCE(n.recovery_status,'Watching') recovery_status,
          (SELECT latitude FROM receptions p WHERE p.protocol=r.protocol AND p.callsign=r.callsign AND latitude IS NOT NULL ORDER BY id DESC LIMIT 1) latitude,
          (SELECT longitude FROM receptions p WHERE p.protocol=r.protocol AND p.callsign=r.callsign AND latitude IS NOT NULL ORDER BY id DESC LIMIT 1) longitude,
          (SELECT received_utc FROM receptions p WHERE p.protocol=r.protocol AND p.callsign=r.callsign AND latitude IS NOT NULL ORDER BY id DESC LIMIT 1) position_utc
          FROM receptions r LEFT JOIN target_notes n ON n.protocol=r.protocol AND n.callsign=r.callsign
          WHERE instr(upper(r.callsign),upper(?))>0 GROUP BY r.protocol,r.callsign ORDER BY last_seen DESC LIMIT 250''',(query,)).fetchall()
    return jsonify([dict(row) for row in rows])


def history_page():
    return page('''<h2>Recovery history</h2><input id="query" placeholder="Search callsign or sonde ID"><button onclick="load()">Search</button><p>History follows the retention policy under Uploads (default: keep forever). Notes and recovery status stay local.</p><div id="targets"></div><script>
const token={{token|tojson}};function dms(v,lat){let x=Math.round(Math.abs(v)*360000);const d=Math.floor(x/360000);x%=360000;return d+'° '+Math.floor(x/6000)+'′ '+((x%6000)/100).toFixed(2)+'″ '+(lat?(v>=0?'N':'S'):(v>=0?'E':'W'));}function el(tag,text){const e=document.createElement(tag);e.textContent=text;return e;}async function load(){const rows=await(await fetch('/api/targets?query='+encodeURIComponent(document.getElementById('query').value))).json();const host=document.getElementById('targets');host.replaceChildren();for(const p of rows){const a=el('article','');a.append(el('b',p.callsign+' · '+p.protocol),el('p',p.packets+' packets · Last heard '+p.last_seen));const args='target='+encodeURIComponent(p.callsign)+'&protocol='+encodeURIComponent(p.protocol);for(const [text,url] of [['CSV','/history.csv?'+args],['GPX','/track.gpx?'+args]]){const link=el('a',text+' ');link.href=url;a.append(link);}if(p.latitude!==null){const link=el('a','Map: '+p.latitude.toFixed(6)+', '+p.longitude.toFixed(6));link.href='https://www.google.com/maps/search/?api=1&query='+encodeURIComponent(p.latitude+','+p.longitude);link.target='_blank';link.rel='noopener';a.append(el('br',''),link,el('p',dms(p.latitude,true)+', '+dms(p.longitude,false)),el('small','Position received: '+p.position_utc));}const state=document.createElement('select');for(const v of ['Watching','Recovery planned','Recovered','Not recoverable'])state.add(new Option(v,v));state.value=p.recovery_status;const note=document.createElement('textarea');note.value=p.note;note.placeholder='Terrain, access, recovery notes…';const save=el('button','Save note');save.onclick=async()=>{const r=await fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({protocol:p.protocol,callsign:p.callsign,note:note.value,recovery_status:state.value})});save.textContent=r.ok?'Saved':'Save failed';};a.append(el('br',''),state,note,save);host.append(a);}}
load();</script>''')


def notes():
    M.csrf();v=request.get_json(silent=True)
    if not isinstance(v,dict):abort(400)
    if v.get('recovery_status') not in ('Watching','Recovery planned','Recovered','Not recoverable'):abort(400)
    if not isinstance(v.get('note'),str) or len(v['note'])>8000:abort(400)
    with M.database() as db:
        if not db.execute('SELECT 1 FROM receptions WHERE protocol=? AND callsign=? LIMIT 1',(v.get('protocol'),v.get('callsign'))).fetchone():abort(404)
        db.execute('INSERT INTO target_notes VALUES (?,?,?,?) ON CONFLICT(protocol,callsign) DO UPDATE SET note=excluded.note,recovery_status=excluded.recovery_status',
                   (v['protocol'],v['callsign'],v['note'],v['recovery_status']))
    return jsonify(ok=True)


def history_query(positions=False):
    target=request.args.get('target');protocol=request.args.get('protocol');conditions=[];values=[]
    if target:conditions.append('callsign=?');values.append(target)
    if protocol:conditions.append('protocol=?');values.append(protocol)
    if positions:conditions.append('latitude IS NOT NULL AND longitude IS NOT NULL')
    return 'SELECT * FROM receptions'+(' WHERE '+' AND '.join(conditions) if conditions else '')+' ORDER BY id',values


def history_csv():
    sql,values=history_query()
    def generate():
        with M.database() as db:
            cursor=db.execute(sql,values);stream=io.StringIO();writer=csv.writer(stream)
            writer.writerow([x[0] for x in cursor.description]);yield stream.getvalue()
            for row in cursor:
                stream.seek(0);stream.truncate();writer.writerow(row);yield stream.getvalue()
    return Response(generate(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=pisonde-history.csv'})


def gpx():
    sql,values=history_query(positions=True)
    def generate():
        yield '<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="PiSonde Mobile" xmlns="http://www.topografix.com/GPX/1/1"><trk><name>Reception track</name><trkseg>'
        with M.database() as db:
            for row in db.execute(sql,values):
                point=ET.Element('trkpt',lat=str(row['latitude']),lon=str(row['longitude']))
                if numeric(row['altitude_m']):ET.SubElement(point,'ele').text=str(row['altitude_m'])
                ET.SubElement(point,'time').text=row['received_utc']
                yield ET.tostring(point,encoding='unicode')
        yield '</trkseg></trk></gpx>'
    return Response(generate(),mimetype='application/gpx+xml',headers={'Content-Disposition':'attachment; filename=pisonde-track.gpx'})


def uploads_page():
    return page('''<h2>Uploads</h2><p>Receiver position is never published. Live packets use your station callsign for attribution.</p>
<label>Station callsign<input id="call" value="{{settings.callsign}}"></label><label><input style="width:auto" id="enabled" type="checkbox" {% if settings.enabled %}checked{% endif %}> Enable uploads</label><button id="save">Save</button>
<p>APRS → APRS-IS; balloon position reports with the balloon symbol and altitude can be imported by SondeHub Amateur. Horus → SondeHub Amateur. DFM/RS41 → local display and history only; direct weather uploads are not supported.</p><p id="state"></p><pre id="destinations" style="white-space:pre-wrap"></pre><p>Historical weather payload archive: <a href="/weather-validation.jsonl?type=DFM">DFM JSONL</a> · <a href="/weather-validation.jsonl?type=RS41">RS41 JSONL</a> · <a href="/upload-report.json">Upload report</a></p><p>These JSONL links export only payloads saved by older releases. No new weather upload payloads are generated. Weather positions remain available locally and in CSV/GPX exports. Pending weather uploads from older releases are disabled, with history retained.</p><button id="test">Test SondeHub Amateur (development mode)</button><p>Development tests do not create live balloon tracks. APRS needs an RF reception to test its iGate path.</p><pre id="results" style="white-space:pre-wrap"></pre>
<fieldset><legend>Storage and retention</legend>
<label>History retention<select id="retention-mode" disabled><option value="forever">Keep forever (default)</option><option value="days">Keep for a number of days</option><option value="low_space">Clean up when storage is low</option></select></label>
<label id="retention-days-label">Retention period<select id="retention-days" disabled><option value="30">30 days</option><option value="60">60 days</option><option value="90">90 days</option><option value="180">180 days</option><option value="365">365 days</option></select></label>
<label id="retention-threshold-label">Clean up below this free space<select id="retention-threshold" disabled><option value="5">5%</option><option value="10">10%</option><option value="15">15%</option><option value="20">20%</option></select></label>
<label><input style="width:auto" type="checkbox" id="retention-protect" checked disabled> Protect latest location per target</label>
<p>Protection keeps the latest location for each target and the latest reception for targets with notes or recovery status. The newest reception is always kept. Low-storage cleanup starts with the oldest records and keeps the most recent five minutes.</p>
<p>Cleanup permanently removes eligible receptions and linked upload/validation payloads from the active database. Existing backups and diagnostic logs follow their own rotation. <a href="/history.csv">Export CSV</a> · <a href="/track.gpx">Export GPX</a> before enabling deletion.</p>
<p>Checks run at startup, about every five minutes, and after saving. Low-space cleanup is bounded and needs temporary SQLite compaction space; it cannot clear unrelated files or guarantee the threshold is reached.</p>
<button type="button" id="retention-save" disabled>Save retention policy</button><p id="retention-feedback" role="status"></p><p id="retention-status"></p></fieldset>

<script>const token={{token|tojson}};let retentionLoaded=false;
const retentionElement=id=>document.getElementById('retention-'+id);
function retentionVisibility(){retentionElement('days-label').hidden=retentionElement('mode').value!=='days';retentionElement('threshold-label').hidden=retentionElement('mode').value!=='low_space';}
retentionElement('mode').onchange=retentionVisibility;retentionVisibility();
function showRetention(s){if(!s)return;const p=s.policy;if(!retentionLoaded){retentionElement('mode').value=p.mode;retentionElement('days').value=p.days;retentionElement('threshold').value=p.threshold;retentionElement('protect').checked=p.protect_latest;retentionVisibility();retentionLoaded=true;for(const id of ['mode','days','threshold','protect','save'])retentionElement(id).disabled=false;}retentionElement('status').textContent=s.free_percent+'% free · '+(s.message||'No cleanup run yet')+(s.last_run?' · Last check '+s.last_run:'')+' · Last removed '+(s.last_deleted||0)+(s.last_error?' · '+s.last_error:'')+(s.settings_error?' · '+s.settings_error:'');}
retentionElement('save').onclick=async()=>{const policy={mode:retentionElement('mode').value,days:Number(retentionElement('days').value),threshold:Number(retentionElement('threshold').value),protect_latest:retentionElement('protect').checked};const confirmed=policy.mode==='forever'?false:confirm('Enable automatic deletion of eligible history and linked upload/validation payloads? Export important data first. Backups may retain older records.');if(policy.mode!=='forever'&&!confirmed)return;const button=retentionElement('save');button.disabled=true;retentionElement('feedback').textContent='Saving…';const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),10000);try{const response=await fetch('/api/retention',{method:'POST',signal:abort.signal,headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({policy,confirmed})});const value=await response.json();if(!response.ok)throw Error(value.error||'Retention save failed');retentionLoaded=false;showRetention(value);retentionElement('feedback').textContent='Retention policy saved. Background cleanup will evaluate it shortly.';}catch(e){retentionElement('feedback').textContent='Save failed: '+e.message;}finally{clearTimeout(timer);button.disabled=false;}};

async function status(){const s=await(await fetch('/api/status',{cache:'no-store'})).json();showRetention(s.retention);document.getElementById('destinations').textContent=Object.entries(s.upload.destinations).map(([name,d])=>name+': '+JSON.stringify(d.counts)+(d.last&&d.last.last_error?' · '+d.last.last_error:'')).join('\\n')+'\\nArchived weather payloads: '+JSON.stringify(s.upload.weather_validation_samples);document.getElementById('state').textContent=s.upload.status+' · '+JSON.stringify(s.upload.counts)+(s.upload.last_success?' · Last sent '+s.upload.last_success+' to '+s.upload.last_destination:'')+(s.upload.last_error?' · '+s.upload.last_error:'');}status();setInterval(status,5000);document.getElementById('save').onclick=async()=>{const r=await fetch('/api/uploads',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({enabled:document.getElementById('enabled').checked,callsign:document.getElementById('call').value})});document.getElementById('state').textContent=r.ok?'Saved':'Save failed';};document.getElementById('test').onclick=async()=>{const b=document.getElementById('test');b.disabled=true;document.getElementById('results').textContent='Testing…';try{const r=await fetch('/api/upload-test',{method:'POST',headers:{'X-CSRF-Token':token}});document.getElementById('results').textContent=JSON.stringify(await r.json(),null,2);}catch(e){document.getElementById('results').textContent=e.message;}finally{b.disabled=false;}};</script>''',settings=settings_snapshot())


def upload_settings():
    M.csrf();v=request.get_json(silent=True)
    if not isinstance(v,dict):abort(400)
    with settings_lock:
        updated=dict(SETTINGS)
        updated.update({key:v[key] for key in ('enabled','callsign') if key in v})
        if isinstance(updated['callsign'],str):updated['callsign']=updated['callsign'].upper().strip()
        try:validate_settings(updated)
        except ValueError as exc:return jsonify(error=str(exc)),400
        M.atomic_json(M.ROOT/'uploads.json',updated)
        SETTINGS.update(updated)
    return jsonify(ok=True)


def development_upload_test():
    M.csrf();s=settings_snapshot();now=utc()
    if not s['enabled'] or not s['dev_uploads']:return jsonify(error='Uploads or development tests are disabled'),400
    base=metadata(s,now)
    base.update(datetime=stamp(now),lat=10.,lon=20.,alt=100.,frame=int(now.timestamp()),
                dev='PiSonde Mobile endpoint test; do not publish')
    cases={'amateur':dict(base,payload_callsign='PISONDE-TEST')}
    results={}
    for destination,payload in cases.items():
        try:
            r=requests.put(ENDPOINTS[destination],data=gzip.compress(json.dumps([payload]).encode()),
                headers={'Content-Type':'application/json','Content-Encoding':'gzip','User-Agent':'PiSonde-Mobile/'+M.VERSION},timeout=(8,10))
            try:
                validation=validate_sondehub_response(r,[payload]);problem=''
            except RuntimeError as exc:validation='rejected_or_unconfirmed';problem=str(exc)
            results[destination]=dict(http_status=r.status_code,response=r.text[:1500],development_only=True,validation=validation,error=problem)
        except Exception as exc:results[destination]=dict(error=str(exc),development_only=True)
    return jsonify(results)


def test_horus_packet():
    """Local WAV test client: only CRC-valid reserved test payload IDs are accepted."""
    M.csrf()
    if request.remote_addr not in ('127.0.0.1','::1'):abort(403)
    value=request.get_json(silent=True)
    if not isinstance(value,dict) or not isinstance(value.get('hex'),str):abort(400)
    packet=M.parse_packet(value['hex'],'HORUS')
    if not packet or not TESTNAME.fullmatch(packet['callsign']):abort(400)
    row=M.store_packet(packet,'HORUS',432501000)
    return jsonify(id=row['id'],callsign=row['callsign'],development_only=True)
