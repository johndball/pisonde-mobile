#!/usr/bin/env python3
"""PiSonde Mobile 0.3.7: one-SDR discovery and durable local reception history.
Uploads and field controls are supplied by field_features.
"""
import atexit
import csv
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import aprslib
from flask import Flask, Response, abort, jsonify, render_template_string, request, session
from waitress import serve
from sdr_health import FlowMonitor, FreshnessMonitor, RadioFailure, Recovery, request_reset

VERSION = '0.3.18'
ROOT = Path(os.environ.get('PISONDE_DATA', '/var/lib/pisonde-mobile'))
APPROOT = Path(os.environ.get('PISONDE_APPROOT', '/opt/pisonde-mobile'))
RUNTIME = Path(os.environ.get('PISONDE_RUNTIME', '/run/pisonde-mobile'))
LOGROOT = Path(os.environ.get('PISONDE_LOG', '/var/log/pisonde-mobile'))
DB = ROOT / 'receptions.sqlite3'
CONFIGFILE = ROOT / 'scanner.json'
WEATHER = APPROOT / 'radiosonde_auto_rx/auto_rx'
VENV = APPROOT / 'venv/bin'
from hardware import load_hardware
DEVICE = load_hardware()['sdr_serial']
APRS_HZ = [144390000]
HORUS_HZ = [432501000, 434200000, 434210000]
PROFILES = ['ALL', 'APRS', 'HORUS', 'HORUS_WIDE', 'DFM', 'RS41', 'MANUAL', 'STOP']
DEFAULT = dict(profile='ALL', manual_mode='APRS', manual_mhz=144.390,
               manual_horus_offset_hz=1000, hold_seconds=65, aprs_dwell=8, horus_dwell=8,
               weather_decode_dwell=25, survey_seconds=3,
               weather_probe_seconds=4, weather_min_mhz=400.0,
               weather_max_mhz=406.0, horus_min_mhz=432.0, horus_max_mhz=435.0,
               peak_db=6.0, max_candidates=8,
               candidates_per_cycle=2, horus_baud=100, gain=38.6)
FRAME = re.compile(r'^\[\d+(?:\.\d+)?\]\s+(.+>[^:]+:.*)$')
DETECT = re.compile(r'^(DFM9|RS41):\s+[-+]?\d')
lock = threading.RLock()
shutdown_event = threading.Event()
revision = 0
skip_once = None
last_watchdog = 0.0
heartbeat_at = time.monotonic()
config = dict(DEFAULT)
radio_recovery = Recovery()
status = dict(version=VERSION, phase='Starting', mode='', frequency_mhz=None,
              hold_remaining=0, error='', last_packet=None, packets=0,
              candidates=[], cycle=0, uploads='Configured by field services', receiver_position='Not published',
              survey_sample_ceiling_hz=2000000,
              survey_backend='private rtl_power, 2.0 MS/s ceiling',
              sdr_state='Starting', sdr_restarts=0, sdr_usb_resets=0, sdr_last_fault='',sdr_sample_bytes=0)

@contextmanager
def database():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA synchronous=FULL')
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()

def atomic_json(path, value):
    temp = path.with_suffix('.new')
    with temp.open('w') as f:
        json.dump(value, f, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def setup():
    global config
    for path in (ROOT, RUNTIME, LOGROOT):
        path.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(
        LOGROOT / 'scanner.log', maxBytes=10*1024*1024, backupCount=29)]
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=handlers, force=True)
    if CONFIGFILE.exists():
        loaded = json.loads(CONFIGFILE.read_text())
        if not isinstance(loaded, dict):
            raise ValueError('scanner.json must be an object')
        config.update({k:v for k,v in loaded.items() if k in DEFAULT})
    validate_config(config)
    # STARTUP_AUTOSCAN_V2: saved holds/stops apply only to the running session.
    if config['profile'] in ('MANUAL','STOP'):
        config['profile']='ALL'
        atomic_json(CONFIGFILE,config)
        logging.info('Startup: cleared saved lock/stop; resuming Auto—all modes')
    with database() as con:
        check=con.execute('PRAGMA quick_check').fetchone()[0]
        if check!='ok':raise RuntimeError('Database integrity check failed; preserve history and inspect backups: '+check)
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS receptions (
          id INTEGER PRIMARY KEY, received_utc TEXT NOT NULL,
          protocol TEXT NOT NULL, frequency_mhz REAL NOT NULL,
          callsign TEXT NOT NULL, latitude REAL, longitude REAL, altitude_m REAL,
          raw_packet TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS receptions_callsign ON receptions(callsign,id);
        ''')
        status['packets'] = con.execute('SELECT COUNT(*) FROM receptions').fetchone()[0]
    # Load dictionaries from existing local source, with no required internet call.
    import horusdemodlib.payloads as payloads
    source = APPROOT / 'horusdemodlib'
    for filename, initializer, attr in [
        ('payload_id_list.txt', payloads.init_payload_id_list, 'HORUS_PAYLOAD_LIST'),
        ('custom_field_list.json', payloads.init_custom_field_list, 'HORUS_CUSTOM_FIELDS')]:
        if (source / filename).exists():
            setattr(payloads, attr, initializer(filename=str(source/filename), nodownload=True))
    logging.info('PiSonde Mobile %s; receiver position unpublished', VERSION)

def validate_frequency(mode, mhz):
    if not isinstance(mhz, (int,float)) or not math.isfinite(mhz):
        return False
    return ((mode == 'APRS' and (144 <= mhz <= 148 or 430 <= mhz <= 450)) or
            (mode == 'HORUS' and 430 <= mhz <= 450) or
            (mode in ('DFM','RS41') and (400 <= mhz <= 406 or 430 <= mhz <= 450)))

def validate_config(c):
    if c['profile'] not in PROFILES or not validate_frequency(c['manual_mode'], c['manual_mhz']):
        raise ValueError('Invalid mode or manual frequency')
    if c['manual_horus_offset_hz'] not in (1000,5000):
        raise ValueError('Invalid Horus IQ offset')
    limits = dict(hold_seconds=(65,600), aprs_dwell=(4,120), horus_dwell=(5,120),
                  weather_decode_dwell=(8,60), survey_seconds=(1,20),
                  weather_probe_seconds=(2,15), peak_db=(3,25), max_candidates=(1,32),
                  candidates_per_cycle=(1,8), horus_baud=(50,1200), gain=(0,49.6))
    for key,(low,high) in limits.items():
        val=c[key]
        if not isinstance(val,(int,float)) or not math.isfinite(val) or not low <= val <= high:
            raise ValueError('Invalid '+key)
    if not 400 <= c['weather_min_mhz'] < c['weather_max_mhz'] <= 406:
        raise ValueError('Weather search must be within 400–406 MHz')
    if not 430 <= c['horus_min_mhz'] < c['horus_max_mhz'] <= 450:
        raise ValueError('Horus search must be within 430–450 MHz')
    for key in ('max_candidates','candidates_per_cycle','horus_baud'):
        if int(c[key]) != c[key]:
            raise ValueError(key+' must be an integer')

def finite_number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def position(lat, lon):
    if finite_number(lat) and finite_number(lon) and -90<=lat<=90 and -180<=lon<=180:
        return float(lat), float(lon)
    return None,None

def parse_packet(line, mode):
    """Accept only decoder-framed APRS, JSON weather, or CRC-valid Horus hex."""
    if mode == 'APRS':
        match=FRAME.match(line)
        if not match:
            return None
        raw=match[1]
        try:
            parsed=aprslib.parse(raw)
        except Exception:
            # Preserve valid AX.25 receptions even if APRS information is unsupported.
            parsed={}
        lat,lon=position(parsed.get('latitude'),parsed.get('longitude'))
        return dict(callsign=raw.split('>',1)[0], latitude=lat, longitude=lon,
                    altitude_m=parsed.get('altitude'), raw_packet=raw)
    if mode in ('DFM','RS41'):
        if not line.startswith('{'):
            return None
        try:
            packet=json.loads(line)
        except ValueError:
            return None
        if not isinstance(packet,dict) or not packet.get('id'):
            return None
        # Native decoder JSON is the decoder's accepted-frame output, not the detector header.
        lat,lon=position(packet.get('lat'),packet.get('lon'))
        return dict(callsign=str(packet['id']),latitude=lat,longitude=lon,
                    altitude_m=packet.get('alt'),raw_packet=line)
    if mode=='HORUS':
        if not re.fullmatch(r'[0-9A-Fa-f]{32,256}',line) or len(line)%2:
            return None
        from horusdemodlib.decoder import decode_packet
        try:
            packet=decode_packet(bytes.fromhex(line),ignore_crc=False)
        except Exception as exc:
            logging.debug('Horus rejected: %s',exc)
            return None
        lat,lon=position(packet.get('latitude'),packet.get('longitude'))
        name=str(packet.get('callsign',packet.get('payload_id','Unknown')))
        return dict(callsign=name,latitude=lat,longitude=lon,altitude_m=packet.get('altitude'),
                    raw_packet=json.dumps(packet,default=str,separators=(',',':')))
    return None

def store_packet(packet, mode, hz):
    alt=packet.get('altitude_m')
    if not finite_number(alt):
        alt=None
    received=datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    with database() as con:
        cur=con.execute('''INSERT INTO receptions
          (received_utc,protocol,frequency_mhz,callsign,latitude,longitude,altitude_m,raw_packet)
          VALUES (?,?,?,?,?,?,?,?)''',
          (received,mode,hz/1e6,packet['callsign'],packet['latitude'],packet['longitude'],alt,packet['raw_packet']))
        ident=cur.lastrowid
    result=dict(packet,id=ident,received_utc=received,protocol=mode,frequency_mhz=hz/1e6,altitude_m=alt)
    with lock:
        status['packets']+=1
        status['last_packet']=result
    logging.info('Stored %s %s %.6f MHz',mode,packet['callsign'],hz/1e6)
    return result

class Pipeline:
    """All SDR subprocesses belong to one owner; bounded shutdown before the next tune."""
    def __init__(self, commands, monitor_samples=None):
        self.commands=commands
        self.processes=[]
        self.output=queue.Queue(maxsize=1024)
        self.threads=[]
        self.flow=None
        self.healthy_since=None
        self.sample_bytes=0
        self.freshness=FreshnessMonitor()
        self.monitor_samples=(Path(commands[0][0]).name=='rtl_fm'
                              if monitor_samples is None else monitor_samples)
    def __enter__(self):
        try:
            previous=None
            for command in self.commands:
                relay=previous is not None and self.monitor_samples and len(self.processes)==1
                p=subprocess.Popen(command,stdin=subprocess.PIPE if relay else previous,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                if relay:
                    t=threading.Thread(target=self._relay,args=(previous,p.stdin),daemon=True)
                    t.start();self.threads.append(t)
                elif previous is not None:
                    previous.close()
                self.processes.append(p)
                if len(self.processes)==1 and self.monitor_samples:
                    self.flow=FlowMonitor(p.pid,counter=lambda _:self.sample_bytes)
                    publish(sdr_state='Waiting for samples',sdr_sample_bytes=0)
                previous=p.stdout
                t=threading.Thread(target=self._read,args=(p.stderr,False),daemon=True)
                t.start();self.threads.append(t)
            t=threading.Thread(target=self._read,args=(previous,True),daemon=True)
            t.start();self.threads.append(t)
            return self
        except BaseException:
            self.close();raise
    def _read(self, pipe, output):
        try:
            for raw in iter(pipe.readline,b''):
                line=raw.decode('utf-8',errors='replace').strip()
                if output:
                    try:
                        self.output.put(line,timeout=.5)
                    except queue.Full:
                        logging.error('Decoder output queue full; dropping line')
                elif line:
                    logging.info('Radio: %s',line)
        finally:
            pipe.close()
    def _relay(self, source, sink):
        # Preserve every sample byte and its order; never inspect or buffer logs
        # as audio. Blocking backpressure is also detected as lack of progress.
        try:
            while True:
                block=os.read(source.fileno(),65536)
                if not block:
                    break
                self.freshness.feed(block)
                remaining=memoryview(block)
                while remaining:
                    written=os.write(sink.fileno(),remaining)
                    self.sample_bytes+=written
                    remaining=remaining[written:]
        except (BrokenPipeError,OSError):
            pass  # Exited decoder or normal bounded pipeline shutdown.
        finally:
            source.close();sink.close()
    def close(self):
        for p in self.processes:
            if p.poll() is None:
                p.terminate()
        deadline=time.monotonic()+2
        for p in self.processes:
            try:
                p.wait(timeout=max(.01,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill();p.wait(timeout=2)
        for t in self.threads:
            t.join(timeout=.3)
    def __exit__(self,*_):
        self.close()
    def ended(self):
        return all(p.poll() is not None for p in self.processes)
    def failed(self):
        return any(p.poll() not in (None,0) for p in self.processes)
    def check_flow(self):
        if any(p.poll() is not None for p in self.processes):
            raise RadioFailure('SDR/decoder pipeline exited; see radio logs')
        if self.flow is None:
            return True
        ready=self.flow.check()
        publish(sdr_sample_bytes=self.sample_bytes)
        fresh,period=self.freshness.check()
        if ready and not fresh:
            publish(sdr_state='Suspected repeating samples' if period else 'Checking sample freshness')
            self.healthy_since=None
        if ready and fresh:
            publish(sdr_state='Samples flowing',error='')
            if self.healthy_since is None:
                self.healthy_since=time.monotonic()
            # A startup burst must not clear failure history while sample flow
            # is already frozen within the six-second watchdog grace period.
            if self.flow.progressed and time.monotonic()-self.healthy_since>=2:
                radio_recovery.healthy()
        return ready and fresh

def commands_for(mode,hz,c):
    base=['rtl_fm','-d',DEVICE,'-g',str(c['gain'])]
    if mode=='APRS':
        return [base+['-f',str(hz),'-M','fm','-s','24000','-'],
                ['stdbuf','-oL','direwolf','-t','0','-c','/etc/pisonde-mobile/aprs.conf',
                 '-n','1','-r','24000','-b','16','-']]
    if mode=='HORUS':
        # Raw IQ, with target's lower tone 1 kHz above IQ centre. Keep this offset internal.
        offset=c.get('horus_offset_hz',1000)
        return [base+['-f',str(hz-offset),'-M','raw','-s','48000','-'],
                [str(VENV/'horus_demod'),'-m','binary','-q','--sample-rate','48000',
                 '--rate',str(int(c['horus_baud'])),'-b','1000' if offset==5000 else '300',
                 '-u','10000' if offset==5000 else '6000']]
    commands=[base+['-f',str(hz),'-M','fm','-F','9','-s','15000','-'],
              ['sox','-t','raw','-r','15000','-e','signed-integer','-b','16','-c','1',
               '-L','-','-r','48000','-b','16','-t','wav','-',
               'highpass','20','lowpass','2000' if mode=='DFM' else '2600']]
    if mode=='DETECT':
        commands.append(['stdbuf','-oL',str(WEATHER/'dft_detect'),'-c','--types','DFM9,RS41'])
    elif mode=='DFM':
        commands.append(['stdbuf','-oL',str(WEATHER/'dfm09mod'),'--ecc','--json','--dist','--auto'])
    elif mode=='RS41':
        commands.append(['stdbuf','-oL',str(WEATHER/'rs41mod'),'--ptu2','--json','--jsnsubfrm1'])
    else:
        raise ValueError('Unsupported decoder '+mode)
    return commands

def heartbeat():
    global last_watchdog,heartbeat_at
    heartbeat_at=time.monotonic()
    address=os.environ.get('NOTIFY_SOCKET')
    if address and heartbeat_at-last_watchdog>10:
        try:
            if address.startswith('@'):address='\0'+address[1:]
            with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
                sock.sendto(b'WATCHDOG=1',address)
            last_watchdog=heartbeat_at
        except OSError:
            logging.warning('Could not notify service watchdog')

def cancelled(rev):
    heartbeat()
    with lock:
        return shutdown_event.is_set() or revision!=rev

def publish(**values):
    with lock:
        status.update(values)

def hold_deadline(now,seconds):
    return now+seconds

def listen(mode,hz,dwell,c,rev,manual=False):
    publish(phase='Starting receiver',mode=mode,frequency_mhz=hz/1e6,hold_remaining=0)
    deadline=time.monotonic()+dwell
    holding=False
    packets=0
    ready=False
    with Pipeline(commands_for(mode,hz,c)) as pipeline:
        while not cancelled(rev):
            now=time.monotonic()
            flowing=pipeline.check_flow()
            if flowing and not ready:
                publish(phase='Listening' if manual else 'Checking')
                ready=True
            if not manual and now>=deadline and flowing:
                break
            if holding:
                publish(hold_remaining=max(0,math.ceil(deadline-now)))
            try:
                line=pipeline.output.get(timeout=.2)
            except queue.Empty:
                if pipeline.ended() or pipeline.failed():
                    raise RadioFailure(mode+' decoder pipeline exited; see logs')
                continue
            packet=parse_packet(line,mode)
            if packet:
                store_packet(packet,mode,hz)
                packets+=1
                if not manual:
                    deadline=hold_deadline(time.monotonic(),c['hold_seconds'])
                    holding=True
                    publish(phase='Tracking',hold_remaining=c['hold_seconds'])
    return packets

def classify(hz,c,rev):
    publish(phase='Identifying signal',mode='DFM / RS41',frequency_mhz=hz/1e6,hold_remaining=0)
    deadline=time.monotonic()+c['weather_probe_seconds']
    with Pipeline(commands_for('DETECT',hz,c)) as pipeline:
        while not cancelled(rev):
            flowing=pipeline.check_flow()
            if time.monotonic()>=deadline and flowing:
                break
            try:
                line=pipeline.output.get(timeout=.2)
            except queue.Empty:
                if pipeline.failed():
                    raise RadioFailure('Weather detector pipeline failed')
                if pipeline.ended():
                    break
                continue
            match=DETECT.match(line)
            if match:
                return 'DFM' if match[1]=='DFM9' else 'RS41'
    return None

def spectrum_candidates(csv_text,c,band="weather"):
    """Rank spectral peaks relative to local noise. Energy is never called a decoded sonde."""
    bins=[]
    minimum=c[band+'_min_mhz']*1e6
    maximum=c[band+'_max_mhz']*1e6
    grid=1000 if band=='horus' else 10000
    separation=7000 if band=='horus' else 15000
    for row in csv.reader(io.StringIO(csv_text)):
        if len(row)<7:
            continue
        try:
            start,end,step=map(float,row[2:5])
            powers=[float(x) for x in row[6:]]
            if step<=0:
                continue
            for i,power in enumerate(powers):
                hz=start+i*step
                if finite_number(power) and minimum<=hz<maximum:
                    bins.append((hz,power,step))
        except ValueError:
            continue
    bins.sort()
    peaks=[]
    for i,(hz,power,step) in enumerate(bins):
        nearby=[p for _,p,_ in bins[max(0,i-60):i+61]]
        if len(nearby)<5:
            continue
        floor=statistics.median(nearby)
        strength=power-floor
        if strength>=c['peak_db'] and power>=max(p for _,p,_ in bins[max(0,i-2):i+3]):
            # A grid is a hint, not a hard assumption. Try measured peak and nearest 10 kHz channel.
            nearest=int(math.floor(hz/grid+.5)*grid)
            peaks.append((strength,nearest,int(round(hz))))
    result=[]
    for strength,nearest,measured in sorted(peaks,reverse=True):
        if not any(abs(nearest-old['hz'])<separation for old in result):
            result.append(dict(hz=nearest,measured_hz=measured,above_floor_db=round(strength,1)))
        if len(result)>=int(c['max_candidates']):
            break
    return result

def survey(c,rev,band="weather"):
    low,high=c[band+'_min_mhz'],c[band+'_max_mhz']
    publish(phase=f'Sweeping {low:g}–{high:g} MHz',sdr_state='Checking spectrum',
            mode='Horus search' if band=='horus' else 'Weather search',
            frequency_mhz=None,hold_remaining=0)
    fd,name=tempfile.mkstemp(prefix='spectrum-',suffix='.csv',dir=RUNTIME)
    os.close(fd)
    path=Path(name)
    try:
        command=[str(APPROOT/'bin/rtl_power'),'-d',DEVICE,'-g',str(c['gain']),'-f',
                 f"{round(low*1e6)}:{round(high*1e6)}:{1000 if band=='horus' else 2000}",
                 '-i',str(c['survey_seconds']),'-1','-c','20%',str(path)]
        with Pipeline([command]) as pipeline:
            deadline=time.monotonic()+c['survey_seconds']+15
            while not cancelled(rev) and not pipeline.ended():
                if pipeline.failed():
                    raise RadioFailure('rtl_power survey failed')
                if time.monotonic()>deadline:
                    raise RadioFailure('rtl_power survey timed out')
                shutdown_event.wait(.15)
            if cancelled(rev):
                return []
            if pipeline.failed():
                raise RadioFailure('rtl_power survey failed')
        content=path.read_text()
        if not content.strip():
            raise RadioFailure('rtl_power returned no spectrum data')
        radio_recovery.healthy()
        publish(sdr_state='Spectrum received',error='')
        peaks=spectrum_candidates(content,c,band)
        publish(candidates=[dict(frequency_mhz=p['hz']/1e6,above_floor_db=p['above_floor_db']) for p in peaks])
        logging.info('%s survey (capture ceiling 2.0 MS/s): %d candidate peaks',band,len(peaks))
        return peaks
    finally:
        path.unlink(missing_ok=True)

def get_skip(mode,hz):
    global skip_once
    with lock:
        if skip_once==(mode,hz):
            skip_once=None
            return True
    return False

def recover_radio(exc,rev):
    # Called outside the Pipeline context: children are closed before a USB reset.
    if cancelled(rev):
        return
    action=radio_recovery.fault(exc)
    publish(phase='Recovering receiver',sdr_state='Stalled',hold_remaining=0,
            error=str(exc),sdr_restarts=radio_recovery.restarts,sdr_last_fault=str(exc))
    logging.error('SDR watchdog: %s; recovery=%s, consecutive failures=%d',
                  exc,action,radio_recovery.failures)
    delay=1 if action=='restart' else 30
    if action=='reset':
        publish(sdr_state='Resetting RTL-SDR')
        try:
            result=request_reset()
            radio_recovery.resets+=1
            publish(sdr_usb_resets=radio_recovery.resets)
            logging.warning('SDR watchdog: %s',result)
            delay=2
        except Exception as reset_error:
            logging.error('SDR watchdog reset unavailable: %s',reset_error)
            publish(error=f'{exc}. {reset_error}. Reconnect the RTL-SDR if this persists.')
    if delay==30:
        publish(sdr_state='Waiting to retry; reconnect SDR if needed')
    deadline=time.monotonic()+delay
    while not cancelled(rev) and time.monotonic()<deadline:
        shutdown_event.wait(.2)

def scanner():
    cursor=0
    while not shutdown_event.is_set():
        heartbeat()
        with lock:
            c=dict(config);rev=revision
            status['cycle']+=1
        profile=c['profile']
        try:
            if profile=='STOP':
                publish(phase='Stopped',mode='',frequency_mhz=None,hold_remaining=0,sdr_state='Stopped',error='')
                shutdown_event.wait(.3)
                continue
            if profile=='MANUAL':
                manual_config=dict(c,horus_offset_hz=c['manual_horus_offset_hz'])
                listen(c['manual_mode'],round(c['manual_mhz']*1e6),0,manual_config,rev,manual=True)
                continue
            if profile in ('ALL','APRS'):
                if not get_skip('APRS',APRS_HZ[0]):
                    listen('APRS',APRS_HZ[0],c['aprs_dwell'],c,rev)
            if cancelled(rev):
                continue
            if profile in ('ALL','HORUS','HORUS_WIDE'):
                for hz in HORUS_HZ:
                    if cancelled(rev):
                        break
                    if not get_skip('HORUS',hz):
                        listen('HORUS',hz,c['horus_dwell'],c,rev)
            if cancelled(rev):
                continue
            if profile=='HORUS_WIDE':
                peaks=survey(c,rev,band='horus')
                if peaks:
                    count=min(int(c['candidates_per_cycle']),len(peaks))
                    selected=[peaks[(cursor+i)%len(peaks)] for i in range(count)]
                    cursor=(cursor+count)%len(peaks)
                    wide=dict(c,horus_offset_hz=5000)
                    for peak in selected:
                        if cancelled(rev):
                            break
                        hz=peak['hz']
                        if not get_skip('HORUS',hz):
                            listen('HORUS',hz,c['horus_dwell'],wide,rev)
            if cancelled(rev):
                continue
            if profile in ('ALL','DFM','RS41'):
                peaks=survey(c,rev)
                if peaks:
                    # Rotate through candidates so persistent strong interferers cannot starve weaker ones.
                    count=min(int(c['candidates_per_cycle']),len(peaks))
                    selected=[peaks[(cursor+i)%len(peaks)] for i in range(count)]
                    cursor=(cursor+count)%len(peaks)
                    for peak in selected:
                        if cancelled(rev):
                            break
                        hz=peak['hz']
                        detected=classify(hz,c,rev)
                        if detected and (profile=='ALL' or profile==detected) and not cancelled(rev):
                            if not get_skip(detected,hz):
                                found=listen(detected,hz,c['weather_decode_dwell'],c,rev)
                                if not found and abs(peak['measured_hz']-hz)>1000 and not cancelled(rev):
                                    # Fall back to measured peak for off-grid or drifting transmitters.
                                    listen(detected,peak['measured_hz'],c['weather_decode_dwell'],c,rev)
                else:
                    shutdown_event.wait(.3)
        except RadioFailure as exc:
            recover_radio(exc,rev)
        except Exception as exc:
            logging.exception('Scanner error')
            publish(phase='Retrying',error=str(exc),hold_remaining=0)
            for _ in range(15):
                if cancelled(rev):
                    break
                shutdown_event.wait(.2)
    publish(phase='Stopped')

app=Flask(__name__)
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',MAX_CONTENT_LENGTH=4096)

def csrf():
    token=request.headers.get('X-CSRF-Token','')
    expected=session.get('token')
    if not expected or not secrets.compare_digest(token,expected):
        abort(403)

@app.get('/')
def home():
    session.setdefault('token',secrets.token_hex(24))
    return render_template_string(PAGE,token=session['token'],version=VERSION)

@app.get('/api/status')
def api_status():
    with lock:
        data=dict(status,config=dict(config),heartbeat_age=round(time.monotonic()-heartbeat_at,1))
    with database() as con:
        rows=con.execute('SELECT * FROM receptions ORDER BY id DESC LIMIT 30').fetchall()
    data.update(recent=[dict(row) for row in rows],free_gib=round(shutil.disk_usage(ROOT).free/2**30,1))
    response=jsonify(data)
    response.headers['Cache-Control']='no-store'
    return response

@app.post('/api/control')
def control():
    global config,revision,skip_once
    csrf()
    incoming=request.get_json(silent=True)
    if not isinstance(incoming,dict):
        abort(400)
    with lock:
        updated=dict(config)
        action=incoming.get('action','select')
        if action=='keep':
            if status['mode'] not in ('APRS','HORUS','DFM','RS41') or not status['frequency_mhz']:
                abort(400,'No active decoder to keep')
            offset=5000 if config['profile']=='HORUS_WIDE' else config.get('manual_horus_offset_hz',1000)
            updated.update(profile='MANUAL',manual_mode=status['mode'],manual_mhz=status['frequency_mhz'],
                           manual_horus_offset_hz=offset)
        elif action=='resume':
            if status['mode'] in ('APRS','HORUS','DFM','RS41') and status['frequency_mhz']:
                skip_once=(status['mode'],round(status['frequency_mhz']*1e6))
            if updated['profile'] in ('MANUAL','STOP'):
                updated['profile']='ALL'
        elif action=='select':
            for key in ('profile','manual_mode','manual_mhz'):
                if key in incoming:
                    updated[key]=incoming[key]
            skip_once=None
        else:
            abort(400,'Invalid action')
        try:
            validate_config(updated)
        except (ValueError,TypeError):
            abort(400,'Invalid mode or frequency')
        atomic_json(CONFIGFILE,updated)
        config=updated
        revision+=1
        status.update(phase='Switching',error='',hold_remaining=0)
    return jsonify(ok=True)

@app.get('/health')
def health():
    return jsonify(version=VERSION,port=80,scanner_alive=bool(worker and worker.is_alive()),heartbeat_age=round(time.monotonic()-heartbeat_at,1))

@app.get('/history.csv')
def history():
    def generate():
        with database() as con:
            cursor=con.execute('SELECT * FROM receptions ORDER BY id')
            buffer=io.StringIO();writer=csv.writer(buffer)
            writer.writerow([col[0] for col in cursor.description]);yield buffer.getvalue()
            for row in cursor:
                buffer.seek(0);buffer.truncate();writer.writerow(row);yield buffer.getvalue()
    return Response(generate(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=pisonde-mobile-history.csv'})

@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    return response

PAGE='''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PiSonde Mobile</title><style>
body{font:18px system-ui;margin:16px;max-width:850px}select,input,button{font:inherit;padding:12px;margin:5px 0;max-width:100%;box-sizing:border-box}
select{width:100%}button{margin-right:6px}article{border-top:1px solid #aaa;padding:12px 0}small{overflow-wrap:anywhere;color:#444}a{padding:6px 0;display:inline-block}.error{color:#a00}#phase{font-weight:bold}#custom{width:100%}
</style></head><body><h2>PiSonde Mobile</h2><p id="phase">Loading…</p><p id="error" class="error"></p><small id="radiohealth"></small>
<label>Search mode<select id="profile">
<option value="ALL">Auto — all modes</option><option value="APRS">APRS — 144.390 MHz</option>
<option value="HORUS">Horus 432.501, 434.200, 434.210</option><option value="HORUS_WIDE">Horus — wide search 432–435 MHz</option><option value="DFM">DFM — search 400–406 MHz</option>
<option value="RS41">RS41 — search 400–406 MHz</option><option value="MANUAL">Manual frequency</option><option value="STOP">Stop receiver</option></select></label>
<div id="manual" hidden><label>Decoder<select id="decoder"><option>APRS</option><option>HORUS</option><option>DFM</option><option>RS41</option></select></label>
<label>Frequency<select id="preset"></select></label><input id="custom" inputmode="decimal" aria-label="Custom frequency in MHz" hidden><small>Custom frequency: type MHz, then tap Done or leave the field.</small></div>
<p><button id="resume">Resume scan</button><button id="keep">Lock to Frequency</button></p>
<small>Auto: resume searching 65 seconds after the last valid packet.<br>Lock to Frequency: lock the current frequency and decoder without a timeout.<br>Resume scan: release the lock; manual locks return to all modes.</small>
<p id="summary"></p><small>Uploads: not configured yet. Receiver location: not published.<br>Horus launch channels: 432.501 · 434.200 · 434.210 MHz (100 baud).<br>Wide Horus mode checks those first, then searches 432–435 MHz.</small>
<details><summary>Search details</summary><p id="peaks"></p><small>Weather search identifies DFM/RS41 candidates before decoding. Wide Horus search tests spectrum peaks with the Horus decoder. Only valid packets trigger the 65-second hold. A signal peak is not a confirmed sonde. One SDR listens on one frequency at a time.</small></details>
<p><a href="/history.csv">Download complete history</a></p><h3>Recent receptions</h3><div id="packets"></div><small>v{{version}}</small>
<script>
const token={{token|tojson}}, $=id=>document.getElementById(id);let initialized=false,busy=false,editing=false;
const presets={APRS:['144.390'],HORUS:['432.501','434.200','434.210'],DFM:['404.000'],RS41:['404.000']};
function dms(value,latitude){let x=Math.round(Math.abs(value)*360000);const d=Math.floor(x/360000);x%=360000;const m=Math.floor(x/6000),s=(x%6000)/100;return `${d}° ${String(m).padStart(2,'0')}′ ${s.toFixed(2).padStart(5,'0')}″ ${latitude?(value>=0?'N':'S'):(value>=0?'E':'W')}`;}
function fill(value){const list=presets[$('decoder').value];$('preset').replaceChildren(...list.map(v=>new Option(v+' MHz',v)),new Option('Custom…','custom'));const match=list.find(v=>Number(v)===Number(value));$('preset').value=match||'custom';$('custom').value=match||value;$('custom').hidden=Boolean(match);}
async function send(values){if(busy)return;busy=true;try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(values)});if(!r.ok)throw Error(await r.text());$('error').textContent='';}catch(e){$('error').textContent=e.message;}finally{busy=false;editing=false;}await refresh();}
function select(){const value={profile:$('profile').value};if(value.profile==='MANUAL'){value.manual_mode=$('decoder').value;value.manual_mhz=Number($('custom').value);if(!Number.isFinite(value.manual_mhz))return;}send(value);}
$('profile').onchange=()=>{$('manual').hidden=$('profile').value!=='MANUAL';select();};
$('decoder').onchange=()=>{fill(presets[$('decoder').value][0]);select();};
$('preset').onchange=()=>{if($('preset').value==='custom'){$('custom').hidden=false;editing=true;$('custom').focus();$('custom').select();}else{$('custom').hidden=true;$('custom').value=$('preset').value;select();}};
$('custom').onfocus=()=>editing=true;$('custom').oninput=()=>editing=true;$('custom').onchange=()=>select();$('custom').onkeydown=e=>{if(e.key==='Enter'){$('custom').blur();}};
$('resume').onclick=()=>send({action:'resume'});$('keep').onclick=()=>send({action:'keep'});
function copyCoordinates(text,button){const field=document.createElement('textarea');field.value=text;document.body.append(field);field.select();field.setSelectionRange(0,text.length);const ok=document.execCommand('copy');field.remove();button.textContent=ok?'Copied':'Select coordinates to copy';}
function element(tag,text){const e=document.createElement(tag);e.textContent=text;return e;}
// PACKET_DOM_REUSE_V1: keep expanded packets and their DOM nodes across polls.
const packetNodes=new Map();
function createPacketNode(p){const a=element('article','');a.append(element('b',p.callsign+' · '+p.protocol+' · '+p.frequency_mhz.toFixed(6)+' MHz'),element('br',''),element('small',p.received_utc+' UTC'),element('br',''));
if(p.latitude!==null&&p.longitude!==null){const link=element('a',p.latitude.toFixed(6)+', '+p.longitude.toFixed(6));link.href='https://www.google.com/maps/search/?api=1&query='+encodeURIComponent(p.latitude+','+p.longitude);link.target='_blank';link.rel='noopener';a.append(link,element('br',''));const d=element('a',dms(p.latitude,true)+', '+dms(p.longitude,false));d.href=link.href;d.target='_blank';d.rel='noopener';const copy=element('button','Copy coordinates');copy.onclick=()=>copyCoordinates(p.latitude.toFixed(6)+', '+p.longitude.toFixed(6),copy);a.append(d,element('br',''),copy,element('br',''));if(p.altitude_m!==null)a.append(element('small','Altitude reported by packet: '+p.altitude_m.toFixed(1)+' m'),element('br',''));}
const details=document.createElement('details');details.append(element('summary','Packet'),element('small',p.raw_packet));a.append(details);return a;}
function renderPackets(rows){
 const host=$('packets'),present=new Set(rows.map(p=>String(p.id)));
 for(const placeholder of host.querySelectorAll('[data-packet-empty]'))placeholder.remove();
 // An expanded older packet stays visible even when it leaves the recent list.
 for(const [key,node] of packetNodes){
  if(!present.has(key)&&!node.querySelector('details').open){node.remove();packetNodes.delete(key);}
 }
 let position=0;
 for(const p of rows){
  const key=String(p.id);let node=packetNodes.get(key);
  if(!node){node=createPacketNode(p);packetNodes.set(key,node);}
  const next=host.children[position];
  if(next!==node)host.insertBefore(node,next||null);
  position++;
 }
 if(!packetNodes.size){const empty=element('p','Waiting for packets.');empty.dataset.packetEmpty='1';host.append(empty);}
}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('Status unavailable');const s=await r.json();
$('phase').textContent=s.phase+(s.mode?' — '+s.mode:'')+(s.frequency_mhz?' · '+s.frequency_mhz.toFixed(6)+' MHz':'')+(s.hold_remaining?' · '+s.hold_remaining+'s hold':'');
$('radiohealth').textContent='SDR: '+s.sdr_state+' · recovery attempts: '+s.sdr_restarts+' · USB resets: '+s.sdr_usb_resets;
if(!busy)$('error').textContent=s.error;$('summary').textContent=s.packets+' stored packets · '+s.free_gib+' GiB free';
if(!busy&&!editing){$('profile').value=s.config.profile;$('decoder').value=s.config.manual_mode;fill(s.config.manual_mhz);$('manual').hidden=s.config.profile!=='MANUAL';initialized=true;}
$('peaks').textContent=s.candidates.length?s.candidates.map(p=>p.frequency_mhz.toFixed(6)+' MHz ('+p.above_floor_db+' dB above local floor)').join(' · '):'No candidate peaks in the latest sweep.';
renderPackets(s.recent);
}catch(e){$('error').textContent=e.message;}}
refresh();setInterval(refresh,2000);
</script></body></html>'''
worker=None

def main():
    global worker
    setup()
    import field_features
    field_features.install(sys.modules[__name__])
    import field_ui
    field_ui.install(sys.modules[__name__])
    key=ROOT/'web-secret'
    if not key.exists():
        key.write_bytes(secrets.token_bytes(32));key.chmod(0o600)
    app.secret_key=key.read_bytes()
    def quit_signal(*_):
        shutdown_event.set()
        raise SystemExit()
    signal.signal(signal.SIGINT,quit_signal);signal.signal(signal.SIGTERM,quit_signal)
    worker=threading.Thread(target=scanner,name='receiver-controller',daemon=False)
    worker.start()
    try:
        serve(app,host='0.0.0.0',port=80,threads=4)
    finally:
        shutdown_event.set();worker.join(timeout=10)

if __name__=='__main__':
    main()
