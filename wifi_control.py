#!/usr/bin/env python3
"""Narrow root-only NetworkManager broker. Local Unix socket; never exposes secrets."""
import glob,json,os,socket,subprocess,time,uuid,threading
from pathlib import Path
from socketserver import ThreadingUnixStreamServer,StreamRequestHandler
from hardware import load_hardware
_hardware=load_hardware()
USB=_hardware['client_mac']
ONBOARD=_hardware['onboard_mac']
AP=_hardware['ap_uuid']
SOCKET='/run/pisonde-control/control.sock'
operation_lock=threading.RLock()

# Physical adapters remain named above; roles are explicit.
AP_MAC=ONBOARD
CLIENT_MAC=USB
ROLE_LAYOUT='onboard-ap-usb-client-v1'

def nm(*args,timeout=12):
    # -g implies terse mode, which normally escapes MAC colons and SSID
    # backslashes. Single-value lookups need literal values for comparisons.
    # Multi-column Wi-Fi scans must retain escaping for fields() to parse them.
    escape='no' if '-g' in args or '--get-values' in args else 'yes'
    result=subprocess.run(['nmcli','--wait','10','--escape',escape,*args],capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('NetworkManager operation failed')
    return result.stdout.strip()

def fields(line):
    output=[];part='';escape=False
    for ch in line:
        if escape:part+=ch;escape=False
        elif ch=='\\':escape=True
        elif ch==':':output.append(part);part=''
        else:part+=ch
    output.append(part)
    return output

def device(mac):
    for path in glob.glob('/sys/class/net/*/address'):
        if Path(path).read_text().strip().upper()==mac:return Path(path).parent.name
    return None

def profiles():
    data=[]
    for ident in nm('-g','UUID','connection','show').splitlines():
        kind=nm('-g','connection.type','connection','show',ident)
        if kind!='802-11-wireless':continue
        ssid=nm('-g','802-11-wireless.ssid','connection','show',ident)
        mac=nm('-g','802-11-wireless.mac-address','connection','show',ident).upper()
        mode=nm('-g','802-11-wireless.mode','connection','show',ident)
        security=nm('-g','802-11-wireless-security.key-mgmt','connection','show',ident)
        data.append(dict(uuid=ident,ssid=ssid,mac=mac,mode=mode,security=security))
    return data

# Independent broker watchdog: network coverage and client count are not faults.
# Journal messages are inspected locally; only fixed reasons/counters enter status.
import collections,logging,re
WIFI_RECOVERY=dict(state='Starting',adapter_attempts=0,service_attempts=0,last_reason='',last_error='')
WIFI_JOURNAL_REASONS={
    'client':('request-scan: could not get scan request result: Timeout',
              'call-disconnect: failed with Timeout','call-remove-network: failed with Timeout'),
    'ap':('brcmf_cyw_mgmt_tx: TX Auth frame operation is failed',)}

class WifiRecovery:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.started=clock();self.seen=set();self.events={'ap':collections.deque(maxlen=40),'client':collections.deque(maxlen=40)}
        self.attempts={'ap':collections.deque(),'client':collections.deque(),'service':collections.deque()}
        self.last_attempt={'ap':float('-inf'),'client':float('-inf'),'service':float('-inf')}
    def ingest(self,rows,ap,client):
        now=self.clock()
        for row in rows:
            cursor=row.get('__CURSOR');message=row.get('MESSAGE','')
            if not isinstance(cursor,str) or cursor in self.seen or not isinstance(message,str):continue
            self.seen.add(cursor)
            if len(self.seen)>4000:self.seen={cursor}
            unit=row.get('_SYSTEMD_UNIT','');kernel=row.get('_TRANSPORT')=='kernel'
            role=None
            if kernel and ap and any(v in message for v in WIFI_JOURNAL_REASONS['ap']):role='ap'
            elif client and unit in ('NetworkManager.service','wpa_supplicant.service') and re.search(r'(?<![\w-])'+re.escape(client)+r'(?![\w-])',message) and any(v in message for v in WIFI_JOURNAL_REASONS['client']):role='client'
            if role:self.events[role].append(now)
        for role in self.events:
            while self.events[role] and now-self.events[role][0]>120:self.events[role].popleft()
    def choose(self,ap,client):
        now=self.clock()
        if now-self.started<90:return None
        for role in self.attempts:
            while self.attempts[role] and now-self.attempts[role][0]>=3600:self.attempts[role].popleft()
        bad={r:len(self.events[r])>=3 for r in self.events}
        # Shared service restart is last resort: both adapters must have fresh
        # fault evidence, and each must already have had an isolated attempt.
        if ap and client and all(bad.values()) and all(self.attempts[r] for r in ('ap','client')) and now-self.last_attempt['service']>=900 and len(self.attempts['service'])<2:
            return 'service'
        for role,present in (('ap',ap),('client',client)):
            if present and bad[role] and now-self.last_attempt[role]>=300 and len(self.attempts[role])<3:return role
        return None
    def record(self,role):
        now=self.clock();self.last_attempt[role]=now;self.attempts[role].append(now)
        for key in self.events:self.events[key].clear()

wifi_recovery=WifiRecovery()

def wifi_journal(since):
    result=subprocess.run(['journalctl','-b','--no-pager','-o','json','--since','@'+str(int(since)),'-n','400',
        '_TRANSPORT=kernel','+','_SYSTEMD_UNIT=NetworkManager.service','+','_SYSTEMD_UNIT=wpa_supplicant.service'],
        capture_output=True,text=True,timeout=8)
    if result.returncode:raise RuntimeError('Wi-Fi recovery journal read failed')
    if len(result.stdout)>2097152:raise RuntimeError('Wi-Fi recovery journal exceeded limit')
    rows=[]
    for line in result.stdout.splitlines():
        try:row=json.loads(line)
        except ValueError:continue
        if isinstance(row,dict):rows.append(row)
    return rows

def recover_wifi(role):
    ap=device(AP_MAC);client=device(CLIENT_MAC)
    if role=='service':
        result=subprocess.run(['/usr/bin/systemctl','restart','wpa_supplicant.service'],capture_output=True,timeout=25)
        if result.returncode:raise RuntimeError('Wi-Fi supplicant restart failed')
        # NetworkManager recreates the interfaces; AP restoration is retried by
        # the next broker tick even when initial activation is not ready yet.
        nm('connection','up',AP,timeout=18)
        return
    interface=ap if role=='ap' else client
    if not interface:return
    # Preserve profiles and hardware bindings. Never create an upstream profile.
    try:nm('device','set',interface,'managed','no')
    finally:nm('device','set',interface,'managed','yes')
    if role=='ap':nm('connection','up',AP,'ifname',interface,timeout=18)
    else:
        saved=[p for p in profiles() if p['uuid']!=AP and p['mac']==CLIENT_MAC and p['mode']!='ap' and p['ssid'] not in forgotten()]
        saved.sort(key=lambda p:int(nm('-g','connection.autoconnect-priority','connection','show',p['uuid']) or 0),reverse=True)
        if saved:nm('connection','up',saved[0]['uuid'],'ifname',interface,timeout=18)

def wifi_health_tick(rows):
    ap=device(AP_MAC);client=device(CLIENT_MAC)
    wifi_recovery.ingest(rows,ap,client)
    role=wifi_recovery.choose(ap,client)
    if role is None:
        WIFI_RECOVERY['state']='Monitoring (90-second startup grace)' if wifi_recovery.clock()-wifi_recovery.started<90 else 'Monitoring'
        return
    if not operation_lock.acquire(blocking=False):return
    try:
        import update_manager as updater
        with updater.locked():
            state=updater.status()
            if updater.power_pending() or (updater.STATE/'pending.json').exists() or (state.get('phase')=='Queued' and not state.get('completed_at')):
                WIFI_RECOVERY['state']='Deferred for update/power operation';return
            wifi_recovery.record(role)
            reason='Both adapters repeatedly failing after isolated recovery' if role=='service' else ('Repeated onboard authentication failures' if role=='ap' else 'Repeated upstream Wi-Fi control timeouts')
            counter='service_attempts' if role=='service' else 'adapter_attempts'
            WIFI_RECOVERY.update(state='Recovering '+role,last_reason=reason,last_error='')
            WIFI_RECOVERY[counter]+=1
            logging.warning('Wi-Fi recovery: %s',reason)
            try:recover_wifi(role)
            except Exception:
                WIFI_RECOVERY.update(state='Recovery attempted; inspect connectivity',last_error='Recovery did not complete; bounded retry applies')
                logging.warning('Wi-Fi recovery operation did not complete')
            else:WIFI_RECOVERY['state']='Recovery attempted; monitoring'
    except RuntimeError:
        WIFI_RECOVERY['state']='Deferred; update lock unavailable'
    finally:operation_lock.release()

def wifi_health_watch():
    # Do not replay previous-boot or pre-start errors after an app update.
    since=time.time()
    while True:
        try:
            started=time.time()
            rows=wifi_journal(since)
            since=started-2
            wifi_health_tick(rows)
            # Recover a missing/inactive AP even if the receiver app is stopped.
            if operation_lock.acquire(blocking=False):
                try:
                    ap=device(AP_MAC)
                    if ap and nm('-g','GENERAL.CON-UUID','device','show',ap)!=AP:
                        import update_manager as updater
                        with updater.locked():
                            state=updater.status()
                            queued=state.get('phase')=='Queued' and not state.get('completed_at')
                            if not queued and not updater.power_pending() and not (updater.STATE/'pending.json').exists():nm('connection','up',AP,timeout=18)
                finally:operation_lock.release()
        except Exception:
            WIFI_RECOVERY.update(state='Monitoring unavailable',last_error='Wi-Fi health check failed; inspect broker journal')
        time.sleep(30)


def status():
    client=device(CLIENT_MAC);ap=device(AP_MAC)
    result=dict(ap_mac=AP_MAC,client_mac=CLIENT_MAC,ap_interface=ap,client_interface=client,onboard_present=bool(ap),usb_present=bool(client),role_layout=ROLE_LAYOUT,ap_present=bool(ap),hotspot='',client_state='',ap_state='',recovery=dict(WIFI_RECOVERY))
    if client:
        result['client_state']=nm('-g','GENERAL.STATE','device','show',client)
        active=nm('-g','GENERAL.CONNECTION','device','show',client)
        result['hotspot']=active if active!='--' else ''
    if ap:result['ap_state']=nm('-g','GENERAL.STATE','device','show',ap)
    return result

FORGOTTEN=Path('/var/lib/pisonde-mobile/forgotten-wifi.json')

def forgotten():
    if not FORGOTTEN.exists():return set()
    data=json.loads(FORGOTTEN.read_text())
    if not isinstance(data,list) or not all(isinstance(s,str) for s in data):
        raise RuntimeError('Saved Wi-Fi removal list is invalid')
    return set(data)

def save_forgotten(values):
    temporary=FORGOTTEN.with_suffix('.new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as out:
        json.dump(sorted(values),out);out.flush();os.fsync(out.fileno())
    os.replace(temporary,FORGOTTEN)

def prefer(ident):
    for p in profiles():
        if p['uuid']!=AP and p['mac']==CLIENT_MAC:
            nm('connection','modify',p['uuid'],'connection.autoconnect-priority',
               '100' if p['uuid']==ident else '20')
    nm('connection','modify',ident,'connection.autoconnect','yes',
       'connection.autoconnect-priority','100')

def ensure():
    blocked=forgotten()
    for p in profiles():
        if p['uuid']!=AP and p.get('ssid') in blocked:
            nm('connection','delete',p['uuid'])
            continue
        desired=AP_MAC if p['uuid']==AP else CLIENT_MAC
        if p['mac']!=desired:
            nm('connection','modify',p['uuid'],'connection.interface-name','',
               '802-11-wireless.mac-address',desired)
        if p['uuid']==AP and p['mode']!='ap':
            nm('connection','modify',AP,'802-11-wireless.mode','ap')
    ap=device(AP_MAC)
    if ap and nm('-g','GENERAL.CON-UUID','device','show',ap)!=AP:
        nm('connection','up',AP,timeout=18)
    return status()

def power_action(value):
    if set(value)!={'action','operation'} or value.get('operation') not in ('restart','shutdown'):
        raise RuntimeError('Power control accepts only restart or shutdown')
    if os.geteuid()!=0:raise RuntimeError('Power control requires the root broker')
    import update_manager as u
    with u.locked():
        if u.power_pending():raise RuntimeError('A device power action is already pending')
        if (u.STATE/'pending.json').exists():raise RuntimeError('Recover the interrupted update before restarting/shutting down')
        state=u.status()
        if state.get('phase')=='Queued' and not state.get('completed_at'):raise RuntimeError('An update is queued; wait for it to finish')
        operation=value['operation'];target='reboot' if operation=='restart' else 'poweroff'
        unit='pisonde-power-'+uuid.uuid4().hex
        u.command(['/usr/bin/systemd-run','--unit='+unit,'--on-active=5s','--collect','/usr/bin/systemctl',target])
        u.put(u.POWER_PENDING,dict(operation=operation,queued_epoch=time.time()))
    return dict(queued=operation,delay_seconds=5)

def handle(value):
    action=value.get('action')
    if action=='power':return power_action(value)
    if action=='update':
        from update_manager import broker_action
        return broker_action(value)
    if action=='sdr_reset':
        if set(value)!={'action'}:raise RuntimeError('SDR reset accepts no device parameters')
        from sdr_health import broker_reset
        return broker_reset()
    if action=='status':return status()
    if action=='ensure':return ensure()
    if action=='saved':
        return [dict(uuid=p['uuid'],ssid=p['ssid']) for p in profiles() if p['uuid']!=AP and p['mac']==CLIENT_MAC and p['ssid'] not in forgotten()]
    if action=='forget':
        if set(value)!={'action','uuid'}:raise RuntimeError('Forget requires only a saved network UUID')
        ident=value.get('uuid')
        rows=profiles()
        target=next((p for p in rows if p['uuid']==ident and p['uuid']!=AP and p['mac']==CLIENT_MAC and p['mode']!='ap'),None)
        if target is None or target['ssid']=='pisondemobile':raise RuntimeError('Only saved upstream networks can be forgotten')
        blocked=forgotten();blocked.add(target['ssid']);save_forgotten(blocked)
        count=0
        for p in rows:
            if p['uuid']!=AP and p['mac']==CLIENT_MAC and p['mode']!='ap' and p['ssid']==target['ssid']:
                nm('connection','delete',p['uuid']);count+=1
        return dict(forgotten=target['ssid'],removed_profiles=count)
    interface=device(CLIENT_MAC)
    if not interface:raise RuntimeError('Linksys upstream Wi-Fi is unavailable; local dashboard remains available')
    if action=='scan':
        output=nm('-t','-f','SSID,SIGNAL,SECURITY','device','wifi','list','ifname',interface,'--rescan','yes',timeout=20)
        found={}
        for line in output.splitlines():
            row=fields(line)
            if len(row)!=3 or not row[0]:continue
            ssid,signal,security=row
            if ssid=='pisondemobile':continue
            if ssid not in found:found[ssid]=dict(ssid=ssid,signal=signal,security=security)
        return list(found.values())
    if action=='saved_connect':
        ident=str(value.get('uuid',''))
        if not any(p['uuid']==ident and p['mac']==CLIENT_MAC and p['uuid']!=AP and p['ssid'] not in forgotten() for p in profiles()):
            raise RuntimeError('Saved hotspot is not bound to Linksys Wi-Fi')
        prefer(ident)
        nm('connection','up',ident,'ifname',interface,timeout=20)
        return status()
    if action=='connect':
        ssid=value.get('ssid','');password=value.get('password','')
        if not isinstance(ssid,str) or not 1<=len(ssid.encode())<=32 or any(c in ssid for c in '\r\n\0'):
            raise RuntimeError('Invalid SSID')
        if not isinstance(password,str) or (password and not 8<=len(password)<=63) or any(c in password for c in '\r\n\0'):
            raise RuntimeError('Password must be empty for open Wi-Fi, or 8–63 characters')
        matching=next((p for p in profiles() if p['ssid']==ssid and p['mac']==CLIENT_MAC and p['uuid']!=AP and (password or not p.get('security'))),None)
        ident=matching['uuid'] if matching else str(uuid.uuid4())
        if not matching:
            nm('connection','add','type','wifi','ifname','*','con-name','PiSonde hotspot '+ssid,
               'connection.uuid',ident,'ssid',ssid,'connection.autoconnect','yes',
               '802-11-wireless.mac-address',CLIENT_MAC)
        args=['connection','modify',ident,'connection.interface-name','',
              '802-11-wireless.mac-address',CLIENT_MAC,'802-11-wireless.cloned-mac-address','permanent',
              '802-11-wireless.mode','infrastructure','802-11-wireless.powersave','2',
              'connection.autoconnect','yes','connection.autoconnect-priority','20']
        if password:args+=['wifi-sec.key-mgmt','wpa-psk','wifi-sec.psk',password]
        nm(*args)
        prefer(ident)
        nm('connection','up',ident,'ifname',interface,timeout=20)
        blocked=forgotten()
        if ssid in blocked:
            blocked.remove(ssid);save_forgotten(blocked)
        return status()
    raise RuntimeError('Unsupported network action')

class Handler(StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(35)
        try:
            raw=self.rfile.readline(8193)
            if len(raw)>8192:raise RuntimeError('Request too large')
            value=json.loads(raw)
            if not isinstance(value,dict):raise RuntimeError('Invalid request')
            with operation_lock:
                response=dict(ok=True,result=handle(value))
        except Exception as exc:
            # Never echo commands, credentials, or complete request bodies.
            response=dict(ok=False,error=str(exc) if isinstance(exc,RuntimeError) else 'Wi-Fi operation failed')
        self.wfile.write(json.dumps(response).encode()+b'\n')

if __name__=='__main__':
    # Update timer migrations must not prevent field Wi-Fi control starting.
    try:
        from update_manager import ensure_support
        ensure_support()
    except Exception as exc:
        import logging
        logging.error('Updater timer setup failed: %s',exc)
    try:ensure()
    except Exception:logging.warning('Initial Wi-Fi guard failed; watchdog will retry')
    threading.Thread(target=wifi_health_watch,name='wifi-health',daemon=True).start()
    Path(SOCKET).unlink(missing_ok=True)
    server=ThreadingUnixStreamServer(SOCKET,Handler)
    os.chmod(SOCKET,0o660)
    import grp
    os.chown(SOCKET,0,grp.getgrnam('pisonde-mobile').gr_gid)
    server.serve_forever()
