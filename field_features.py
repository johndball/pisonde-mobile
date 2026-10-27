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
ENDPOINTS={'amateur':'https://api.v2.sondehub.org/amateur/telemetry',
           'sondehub':'https://api.v2.sondehub.org/sondes/telemetry'}
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
    if mode not in ('DFM','RS41'):raise ValueError('Unsupported telemetry type')
    if raw.get('encrypted'):raise ValueError('Encrypted payload')
    if not isinstance(raw.get('id'),str):raise ValueError('Missing sonde ID')
    when=parse_datetime(raw.get('datetime'),received)
    if abs((when-received).total_seconds())>180:raise ValueError('Payload/receiver time differs by more than 3 minutes')
    lat,lon=raw.get('lat'),raw.get('lon');alt=raw.get('alt')
    if not all(numeric(x) for x in (lat,lon,alt)) or not -90<=lat<=90 or not -180<=lon<=180 or not -500<=alt<=60000:
        raise ValueError('Invalid payload position')
    if lat==0 and lon==0:raise ValueError('Zero position')
    if 'sats' in raw and (not numeric(raw['sats']) or raw['sats']<4):raise ValueError('Insufficient GPS satellites')
    ident=raw['id']
    if mode=='RS41':
        if not re.fullmatch(r'[A-Z][0-9]{7}',ident):raise ValueError('Unconfirmed RS41 serial')
        out.update(manufacturer='Vaisala',type='RS41',serial=ident)
        frame=raw.get('frame')
        if not isinstance(frame,int) or isinstance(frame,bool) or frame<0:raise ValueError('Invalid RS41 frame')
    else:
        if not re.fullmatch(r'DFM-\d{5,12}',ident):raise ValueError('Unconfirmed DFM serial')
        out.update(manufacturer='Graw',type='DFM',serial=ident.split('-',1)[1])
        frame=int(when.timestamp())
        if 'dfmcode' in raw:out['dfmcode']=raw['dfmcode']
        subtype=raw.get('subtype')
        if isinstance(subtype,str):
            subtype=subtype.split(':',1)[-1]
            if subtype in ('DFM06','DFM09','DFM09P','DFM17'):out['subtype']=subtype
    out.update(frame=frame,datetime=stamp(when),lat=lat,lon=lon,alt=alt,frequency=row['frequency_mhz'])
    # Keep sonde-provided time and reference fields; never substitute receipt time.
    out.update(lat=round(lat,6),lon=round(lon,6),alt=round(alt,2))
    bounds={'temp':(-273.,100.),'humidity':(0.,100.),'pressure':(0.,1200.),
            'vel_h':(0.,350.),'vel_v':(-150.,150.),'heading':(0.,360.),
            'sats':(0.,100.),'batt':(0.,100.),'snr':(-100.,100.)}
    for key,(low,high) in bounds.items():
        value=raw.get(key)
        if numeric(value) and low<=value<=high:
            if key=='temp' and value<=-273.:continue
            if key=='sats':
                if not float(value).is_integer():continue
                out[key]=int(value)
            else:out[key]=round(value,2)
    for key in ('ref_position','ref_datetime'):
        if isinstance(raw.get(key),str):out[key]=raw[key]
    if mode=='RS41':
        if raw.get('subtype') in ('RS41-SG','RS41-SGP','RS41-SGM'):out['subtype']=raw['subtype']
        if isinstance(raw.get('bt'),int) and not isinstance(raw['bt'],bool) and raw['bt']>=0:out['burst_timer']=raw['bt']
        if isinstance(raw.get('aux'),str) and re.fullmatch(r'(?:[0-9a-fA-F]{2})+',raw['aux']):out['xdata']=raw['aux']
    return 'sondehub',out,False


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

def add_job(row):
    with settings_lock:s=dict(SETTINGS)
    if not s['enabled']:return
    received=isoparse(row['received_utc']).astimezone(timezone.utc)
    try:
        if row['protocol']=='APRS':
            payload=aprs_gate(row['raw_packet'],s['callsign'])
            if payload is None:return
            destination='aprs'
        else:
            destination,payload,test=format_sonde(row,s)
            if not test:
                # Require recurring weather serials and reject implausible jumps before upload.
                if row['protocol'] in ('DFM','RS41'):
                    with M.database() as db:
                        candidates=db.execute('''SELECT * FROM receptions
                           WHERE protocol=? AND callsign=? AND id<? AND latitude IS NOT NULL
                           ORDER BY id DESC LIMIT 30''', (row['protocol'],row['callsign'],row['id'])).fetchall()
                    current_key=(payload['frame'],payload['datetime'])
                    prev=[];seen={current_key}
                    for candidate in candidates:
                        try:
                            _,previous_payload,_=format_sonde(candidate,s)
                            key=(previous_payload['frame'],previous_payload['datetime'])
                            if key in seen:continue
                            seen.add(key);prev.append(candidate)
                            if len(prev)==2:break
                        except (ValueError,TypeError,KeyError):continue
                    if len(prev)<2:raise ValueError('Waiting for three distinct position frames for this sonde')
                    for point in prev:
                        dt=abs((received-isoparse(point['received_utc'])).total_seconds())
                        if dt>120:raise ValueError('Waiting for fresh serial confirmation')
                        lat,lon=row['latitude'],row['longitude']
                        distance=111320*math.hypot(lat-point['latitude'],(lon-point['longitude'])*math.cos(math.radians(lat)))
                        if distance/max(dt,1)>350:raise ValueError('Implausible position jump')
                        if numeric(point['altitude_m']) and abs(row['altitude_m']-point['altitude_m'])/max(dt,1)>150:
                            raise ValueError('Implausible altitude jump')
            if row['protocol'] in ('DFM','RS41'):
                with M.database() as db:
                    duplicate=db.execute('''SELECT 1 FROM upload_jobs j JOIN receptions r ON r.id=j.reception_id
                         WHERE j.destination='sondehub' AND r.protocol=? AND r.callsign=?
                         AND json_extract(j.payload,'$.frame')=? AND json_extract(j.payload,'$.datetime')=? LIMIT 1''',
                         (row['protocol'],row['callsign'],payload['frame'],payload['datetime'])).fetchone()
                if duplicate:return
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
        rows=db.execute("SELECT * FROM upload_jobs WHERE status='pending' AND next_attempt<=? ORDER BY id LIMIT 100",(time.time(),)).fetchall()
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
            else:
                payload=[json.loads(x['payload']) for x in items]
                body=gzip.compress(json.dumps(payload,allow_nan=False).encode())
                response=http.put(ENDPOINTS[destination],data=body,headers={
                    'Content-Type':'application/json','Content-Encoding':'gzip',
                    'User-Agent':'PiSonde-Mobile/'+M.VERSION},timeout=(8,10))
                if response.status_code not in (200,201,202):
                    raise RuntimeError('HTTP '+str(response.status_code)+': '+response.text[:300])
                result=validate_sondehub_response(response,payload)
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


def install(module,start_threads=True):
    global M
    M=module;load_settings();create_schema()
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
        data['storage_warning']='Low storage: export history and free space.' if data['free_gib']<5 else ''
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
    M.app.add_url_rule('/api/uploads','field_uploads_api',upload_settings,methods=['POST'])
    M.app.add_url_rule('/api/upload-test','field_test',development_upload_test,methods=['POST'])
    M.app.add_url_rule('/api/test-horus','field_test_horus',test_horus_packet,methods=['POST'])
    M.PAGE=M.PAGE.replace('<p id="summary"></p>', '<p id="summary"></p><p id="upload-status"></p><p><a href="/wifi">Wi-Fi</a> · <a href="/history">Recovery history</a> · <a href="/uploads">Uploads</a></p>')
    M.PAGE=M.PAGE.replace('Uploads: not configured yet. Receiver location: not published.', 'Receiver location: not published.')
    M.PAGE=M.PAGE.replace("const fragment=document.createDocumentFragment();", "$('upload-status').textContent=s.upload.status+' · queued '+(s.upload.counts.pending||0)+' · SondeHub accepted '+(s.upload.counts.accepted||0)+' · APRS sent '+(s.upload.counts.sent||0)+' · DEV tests validated '+(s.upload.counts.test_passed||0)+(s.storage_warning?' · '+s.storage_warning:'');const fragment=document.createDocumentFragment();")
    if start_threads:
        threading.Thread(target=uploader,name='uploads',daemon=True).start()
        threading.Thread(target=wifi_watch,name='wifi-watchdog',daemon=True).start()


def token():
    from flask import session
    session.setdefault('token',__import__('secrets').token_hex(24));return session['token']

BASE='''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>PiSonde Mobile</title><style>
body{font:18px system-ui;margin:16px;max-width:850px}input,button,select,textarea{font:inherit;padding:10px;margin:5px 0;max-width:100%;box-sizing:border-box}input,select,textarea{width:100%}article{border-top:1px solid #aaa;padding:12px 0}small{overflow-wrap:anywhere}a{display:inline-block;padding:6px 0}.error{color:#a00}</style></head><body><a href="/">← Receiver</a> CONTENT </body></html>'''

def page(content,**data):return render_template_string(BASE.replace('CONTENT',content),token=token(),**data)


def wifi_page():
    return page('''<h2>Wi-Fi hotspots</h2><p>The onboard radio keeps pisondemobile available. The Linksys USB radio joins Internet hotspots.</p>
<p id="state"></p><p id="adapters"></p><button onclick="load()">Refresh / scan</button><label>Nearby hotspots<select id="nearby"><option value="">Select or type below</option></select></label>
<label>Hotspot name<input id="ssid" maxlength="32" autocomplete="off"></label><label>Password<input id="password" type="password" autocomplete="new-password"></label><button id="connect">Connect and save</button>
<h3>Saved hotspots</h3><div id="saved"></div><p id="message"></p><script>
const token={{token|tojson}},$=id=>document.getElementById(id);async function call(v){const r=await fetch('/api/wifi',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(v)});const d=await r.json();if(!r.ok)throw Error(d.error||'Wi-Fi request failed');return d;}
async function load(confirmation='',rescan=true){try{
$('message').textContent=rescan?'Scanning…':'Updating connection status…';
const s=await call({action:'status'});
$('adapters').textContent='Onboard AP MAC: '+s.ap_mac+' ('+(s.ap_interface||'missing')+') · Linksys upstream MAC: '+s.client_mac+' ('+(s.client_interface||'unplugged')+')';$('state').textContent='Upload connection: '+(s.hotspot||'offline')+' · Onboard AP: '+(s.ap_present?'present':'missing');
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
    return page('''<h2>Recovery history</h2><input id="query" placeholder="Search callsign or sonde ID"><button onclick="load()">Search</button><p>History is retained indefinitely. Notes and recovery status stay local.</p><div id="targets"></div><script>
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
<p>APRS → APRS-IS; balloon position reports with the balloon symbol and altitude can be imported by SondeHub Amateur. Horus → SondeHub Amateur. DFM/RS41 → SondeHub.</p><p id="state"></p><pre id="destinations" style="white-space:pre-wrap"></pre><p>Weather validation downloads: <a href="/weather-validation.jsonl?type=DFM">DFM JSONL</a> · <a href="/weather-validation.jsonl?type=RS41">RS41 JSONL</a> · <a href="/upload-report.json">Upload report</a></p><p>Weather publishing requires SondeHub approval of PiSonde-Mobile for each sonde type. Collect actual flight data for review; an empty JSONL file means no eligible samples have been queued yet. Exporting never retransmits old packets.</p><button id="test">Test SondeHub endpoints (development mode)</button><p>Development tests are validated without creating live balloon tracks. APRS needs an RF reception to test its iGate path.</p><pre id="results" style="white-space:pre-wrap"></pre>
<script>const token={{token|tojson}};async function status(){const s=await(await fetch('/api/status',{cache:'no-store'})).json();document.getElementById('destinations').textContent=Object.entries(s.upload.destinations).map(([name,d])=>name+': '+JSON.stringify(d.counts)+(d.last&&d.last.last_error?' · '+d.last.last_error:'')).join('\\n')+'\\nWeather validation samples: '+JSON.stringify(s.upload.weather_validation_samples);document.getElementById('state').textContent=s.upload.status+' · '+JSON.stringify(s.upload.counts)+(s.upload.last_success?' · Last sent '+s.upload.last_success+' to '+s.upload.last_destination:'')+(s.upload.last_error?' · '+s.upload.last_error:'');}status();setInterval(status,5000);document.getElementById('save').onclick=async()=>{const r=await fetch('/api/uploads',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({enabled:document.getElementById('enabled').checked,callsign:document.getElementById('call').value})});document.getElementById('state').textContent=r.ok?'Saved':'Save failed';};document.getElementById('test').onclick=async()=>{const b=document.getElementById('test');b.disabled=true;document.getElementById('results').textContent='Testing…';try{const r=await fetch('/api/upload-test',{method:'POST',headers:{'X-CSRF-Token':token}});document.getElementById('results').textContent=JSON.stringify(await r.json(),null,2);}catch(e){document.getElementById('results').textContent=e.message;}finally{b.disabled=false;}};</script>''',settings=settings_snapshot())


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
    cases={'amateur':dict(base,payload_callsign='PISONDE-TEST'),
           'sondehub':dict(base,manufacturer='Vaisala',type='RS41',serial='T0000000'),
           'sondehub_dfm':dict(base,manufacturer='Graw',type='DFM',subtype='DFM17',serial='123456789')}
    results={}
    for destination,payload in cases.items():
        try:
            r=requests.put(ENDPOINTS['sondehub' if destination=='sondehub_dfm' else destination],data=gzip.compress(json.dumps([payload]).encode()),
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
