#!/usr/bin/env python3
"""Controlled PiSonde Mobile 0.3.17 recovery and field-service verification.
Use --field-only for nondisruptive field checks.
Run the full test with sudo on the installed host while manually locked to APRS. No RF transmission
or internet access is required. Pauses only unit-owned rtl_fm processes using
pidfds; finally resumes any paused process which the watchdog has not replaced.
"""
import csv
from datetime import datetime, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

UNIT='pisonde-mobile.service'
BASE='http://127.0.0.1'
paused=[]
main_pid=None
initial_config=None

class CheckFailed(RuntimeError):pass

def run(args,timeout=10):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        raise CheckFailed('Command failed: '+' '.join(args[:4])+' '+result.stderr.strip()[:200])
    return result.stdout.strip()

def get(path):
    with urllib.request.urlopen(BASE+path,timeout=3) as response:return response.read()

def state():
    value=json.loads(get('/api/status'))
    if initial_config is not None and value.get('config')!=initial_config:
        raise CheckFailed('Receiver settings changed during the test; stopping.')
    return value

def service_pid():return int(run(['systemctl','show',UNIT,'--property=MainPID','--value']))

def owned_receiver(pid):
    root=Path('/proc')/str(pid)
    try:
        cmd=(root/'cmdline').read_bytes().split(b'\0')
        groups=(root/'cgroup').read_text().splitlines()
        return bool(cmd and Path(os.fsdecode(cmd[0])).name=='rtl_fm'
                    and any(line.split(':',2)[-1].endswith('/'+UNIT) for line in groups))
    except (FileNotFoundError,ProcessLookupError):return False

def receiver():
    result=subprocess.run(['pgrep','-x','rtl_fm'],capture_output=True,text=True,timeout=3)
    if result.returncode not in (0,1):raise CheckFailed('Could not enumerate receiver processes')
    found=[int(pid) for pid in result.stdout.split() if owned_receiver(int(pid))]
    if len(found)>1:raise CheckFailed('Multiple unit-owned SDR readers found; test refused.')
    return found[0] if found else None

def pause(pid):
    if service_pid()!=main_pid:raise CheckFailed('Main service restarted unexpectedly')
    fd=os.pidfd_open(pid)
    try:
        if not owned_receiver(pid):raise CheckFailed('Receiver identity changed; no signal sent')
        paused.append(fd)
        signal.pidfd_send_signal(fd,signal.SIGSTOP)
    except BaseException:
        if fd not in paused:os.close(fd)
        raise
    print(f'Paused unit-owned rtl_fm PID {pid}; waiting for watchdog.',flush=True)

def next_receiver(old,seconds=25):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        pid=receiver()
        if pid is not None and pid!=old:return pid
        time.sleep(.12)
    raise CheckFailed('Watchdog did not replace the paused SDR process within 25 seconds')

def wait_flow(seconds=35):
    deadline=time.monotonic()+seconds
    previous=None
    while time.monotonic()<deadline:
        s=state();pid=receiver()
        if service_pid()!=main_pid:raise CheckFailed('Main service restarted instead of recovering the SDR pipeline')
        count=s.get('sdr_sample_bytes',0)
        if pid and s.get('sdr_state')=='Samples flowing' and count>0:
            if previous and previous[0]==pid and count>previous[1]:return s
            previous=(pid,count)
        else:previous=None
        time.sleep(.4)
    raise CheckFailed('Sample flow did not resume: '+json.dumps(state(),default=str)[:600])

def resume_paused():
    for fd in paused:
        try:signal.pidfd_send_signal(fd,signal.SIGCONT)
        except ProcessLookupError:pass
        finally:os.close(fd)
    paused.clear()

def verify_recovery():
    baseline=wait_flow()
    restart0=baseline['sdr_restarts'];reset0=baseline['sdr_usb_resets']
    original=receiver()
    print('TEST 1: one silent sample stall -> pipeline restart.',flush=True)
    pause(original);next_receiver(original)
    after=wait_flow()
    if after['sdr_restarts']<=restart0:raise CheckFailed('Recovery counter did not increase')
    if after['sdr_usb_resets']!=reset0:raise CheckFailed('Unexpected USB reset during single-fault test')
    time.sleep(3)  # Allow the scanner to classify the replacement stream as healthy.
    wait_flow()
    print('PASS: sample flow resumed on a replacement pipeline; frequency lock preserved.',flush=True)
    print('TEST 2: two consecutive stalls -> targeted RTL-SDR USB reset.',flush=True)
    old=receiver();pause(old)
    new=next_receiver(old)
    # Freeze immediately, before two seconds of healthy samples clear failure history.
    pause(new)
    deadline=time.monotonic()+50
    while time.monotonic()<deadline:
        s=state()
        if s.get('sdr_usb_resets',0)>reset0:break
        time.sleep(.4)
    else:raise CheckFailed('Targeted USB reset not confirmed. Status: '+json.dumps(state(),default=str)[:600])
    after=wait_flow()
    print('PASS: targeted USB reset completed and samples resumed automatically.',flush=True)
    print('Recovery attempts:',after['sdr_restarts'],'Successful USB resets:',after['sdr_usb_resets'])
    if after['packets']<baseline['packets']:raise CheckFailed('Stored packet count decreased')
    return baseline

def verify_field(baseline):
    print('FIELD CHECKS:',flush=True)
    # Read SQLite as its service account to avoid root-owned WAL/SHM sidecars.
    code='''import json,sqlite3
c=sqlite3.connect("file:/var/lib/pisonde-mobile/receptions.sqlite3?mode=ro",uri=True)
try:
 print(json.dumps(dict(integrity=c.execute("PRAGMA quick_check").fetchone()[0],packets=c.execute("SELECT count(*) FROM receptions").fetchone()[0],positions=c.execute("SELECT count(*) FROM receptions WHERE latitude IS NOT NULL AND longitude IS NOT NULL").fetchone()[0])))
finally:c.close()
'''
    db=json.loads(run(['runuser','-u','pisonde-mobile','--','/usr/bin/python3','-c',code]))
    if db['integrity']!='ok' or db['packets']<baseline['packets'] or not db['positions']:
        raise CheckFailed('History validation failed: '+json.dumps(db))
    print(f"PASS: local history integrity; {db['packets']} packets, {db['positions']} with coordinates.")
    with urllib.request.urlopen(BASE+'/history.csv',timeout=15) as response:
        rows=sum(1 for _ in csv.DictReader(io.TextIOWrapper(response,encoding='utf-8')))
    if rows<baseline['packets']:raise CheckFailed('CSV export is missing reception history')
    with urllib.request.urlopen(BASE+'/track.gpx',timeout=15) as response:
        points=sum(1 for _,element in ET.iterparse(response,events=('end',))
                   if element.tag.rsplit('}',1)[-1]=='trkpt')
    if not points:raise CheckFailed('GPX export contains no positions')
    print(f'PASS: CSV exports {rows} packets; GPX exports {points} positions.')
    for unit in (UNIT,'pisonde-wifi-control.service','pisonde-history-backup.timer'):
        if run(['systemctl','is-active',unit])!='active':raise CheckFailed(unit+' is not active')
        if run(['systemctl','is-enabled',unit])!='enabled':raise CheckFailed(unit+' is not enabled at boot')
    print('PASS: receiver, Wi-Fi broker and backup timer active and enabled at boot.')
    run(['systemctl','start','pisonde-history-backup.service'],timeout=60)
    backup=Path('/var/backups/pisonde-mobile/history/history-latest.sqlite3')
    if not backup.is_file() or backup.stat().st_size==0:raise CheckFailed('Checked backup is missing')
    print('PASS: fresh SQLite backup completed and validated by the backup service.')
    # Read bindings only: do not switch hotspots or expose Wi-Fi passwords.
    sys.path.insert(0,'/opt/pisonde-mobile')
    import wifi_control as wifi
    profiles=wifi.profiles()
    aps=[p for p in profiles if p['uuid']==wifi.AP]
    if len(aps)!=1 or aps[0]['mac']!=wifi.AP_MAC or aps[0]['mode']!='ap':
        raise CheckFailed('Onboard AP profile binding is incorrect: '+json.dumps([dict(uuid=p['uuid'],mac=p['mac'],mode=p['mode']) for p in profiles]))
    if any(p['mac']!=wifi.CLIENT_MAC for p in profiles if p['uuid']!=wifi.AP):
        raise CheckFailed('A client Wi-Fi profile is not bound to onboard Wi-Fi')
    print('PASS: Onboard Wi-Fi bound to AP; saved client profiles bound to USB upstream Wi-Fi.')
    print('POWER:',run(['vcgencmd','get_throttled']))
    print('TEMPERATURE:',run(['vcgencmd','measure_temp']))
    final=state()
    print('FINAL:',json.dumps({key:final.get(key) for key in ('version','phase','mode','frequency_mhz','packets','sdr_state','sdr_restarts','sdr_usb_resets','error')}))

def main():
    global main_pid,initial_config
    if sys.argv[1:]==['--help']:
        print(__doc__);return 0
    field_only=sys.argv[1:]==['--field-only']
    if sys.argv[1:] and not field_only:raise CheckFailed('Use --field-only or --help, or no arguments for the full interruption test')
    if os.geteuid()!=0:
        raise CheckFailed('Run with sudo on the installed PiSonde Mobile host')
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
        raise CheckFailed('Python pidfd support is required; no processes changed')
    with open('/run/lock/pisonde-recovery-verification.lock','w') as guard:
        try:fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise CheckFailed('Another recovery verification is running')
        before=state()
        if before.get('version')!='0.3.17':raise CheckFailed('Install PiSonde Mobile 0.3.17 before verification')
        if not field_only and (before.get('config',{}).get('profile')!='MANUAL'
            or before.get('config',{}).get('manual_mode')!='APRS' or before.get('mode')!='APRS'):
            raise CheckFailed('Select APRS and Lock to Frequency before running this 0.3.17 test')
        # Enforce pre-existing reset limits before inducing a fault.
        history=Path('/run/pisonde-sdr-reset-history.json')
        if not field_only and history.exists():
            now=time.monotonic();times=[float(t) for t in json.loads(history.read_text()) if 0<=now-float(t)<3600]
            if times and now-max(times)<120:
                raise CheckFailed(f'Reset cooldown active; rerun in {int(121-now+max(times))} seconds')
            if len(times)>=3:raise CheckFailed('Three USB reset attempts already occurred this hour; defer this test')
        initial_config=before['config'];main_pid=service_pid()
        baseline=before if field_only else verify_recovery()
        resume_paused()
        verify_field(baseline)
        print('PASS: field closeout checks completed.' if field_only else 'PASS: automatic pipeline recovery, USB-reset recovery and field checks completed.')
        print('Now replay the APRS WAV once to confirm reception and uploads after recovery.')
    return 0

if __name__=='__main__':
    result=1
    try:result=main()
    except (Exception,KeyboardInterrupt) as exc:
        print('FAIL:',str(exc) or 'Interrupted',flush=True)
        try:
            s=state();print('SDR status:',s.get('sdr_state'),'Error:',s.get('error'))
        except Exception:pass
        print('If reception remains stopped: sudo systemctl restart pisonde-mobile.service')
    finally:resume_paused()
    sys.exit(result)
