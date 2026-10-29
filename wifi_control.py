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

# Fixed-target USB helper. No caller-supplied bus, port, path or MAC is accepted.
NET_ROOT=Path('/sys/class/net')
USB_ROOT=Path('/sys/bus/usb/devices')
USB_DEV_ROOT=Path('/dev/bus/usb')
CLIENT_MANAGEMENT_RESTORE=False

def client_usb_target():
    if CLIENT_MAC==AP_MAC:raise RuntimeError('Upstream and AP identities must differ')
    matches=[]
    for address in NET_ROOT.glob('*/address'):
        try:
            if address.read_text().strip().upper()!=CLIENT_MAC:continue
            network_device=(address.parent/'device').resolve(strict=True)
            parents=[v for v in (network_device,*network_device.parents) if (v/'idVendor').is_file()]
            if not parents:continue
            target=parents[0]
            if not any(v.resolve()==target for v in USB_ROOT.iterdir()):continue
            if (target/'idVendor').read_text().strip().lower()!='13b1' or (target/'idProduct').read_text().strip().lower()!='003e':continue
            if (target/'bDeviceClass').read_text().strip().lower()=='09':continue
            bus=int((target/'busnum').read_text());number=int((target/'devnum').read_text())
            if not 1<=bus<=255 or not 1<=number<=127:continue
            matches.append((target,USB_DEV_ROOT/f'{bus:03d}'/f'{number:03d}',(bus-1)*128+number-1))
        except (OSError,ValueError):continue
    if len(matches)!=1:raise RuntimeError('Exactly one configured Linksys AE6000 USB target is required')
    return matches[0]

def reset_client_usb():
    import fcntl,stat
    if os.geteuid()!=0:raise RuntimeError('USB Wi-Fi reset requires root')
    target,path,minor=client_usb_target()
    fd=os.open(path,os.O_WRONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISCHR(info.st_mode) or os.major(info.st_rdev)!=189 or os.minor(info.st_rdev)!=minor:
            raise RuntimeError('USB device node does not match the configured upstream adapter')
        if client_usb_target()!=(target,path,minor):raise RuntimeError('USB Wi-Fi identity changed before reset')
        fcntl.ioctl(fd,0x5514,0)  # USBDEVFS_RESET: device-level reset, not port power cycling.
    finally:os.close(fd)
    return dict(message='Targeted Linksys USB reset completed; reconnecting upstream')

def restore_client_management():
    global CLIENT_MANAGEMENT_RESTORE
    if not CLIENT_MANAGEMENT_RESTORE:return True
    current=device(CLIENT_MAC)
    if not current:return False
    nm('device','set',current,'managed','yes')
    CLIENT_MANAGEMENT_RESTORE=False
    return True

def request_client_usb_reset():
    result=subprocess.run(['/usr/bin/python3',str(Path(__file__).resolve()),'--reset-client-usb'],
        capture_output=True,text=True,timeout=12)
    if result.returncode:raise RuntimeError('Targeted Linksys USB reset failed; replug the Linksys if needed')
    return json.loads(result.stdout)

def client_link(interface):
    if not interface:return dict(client_state='',client_connected=False,client_note='USB Wi-Fi unplugged',hotspot='',attempted_hotspot='',client_uuid='',has_ipv4=False,has_gateway=False)
    raw=nm('-t','-f','GENERAL.STATE,GENERAL.CONNECTION,GENERAL.CON-UUID,IP4.ADDRESS,IP4.GATEWAY','device','show',interface,timeout=4)
    data={}
    for line in raw.splitlines():
        row=fields(line)
        if len(row)==2:data[row[0]]=row[1]
    state=data.get('GENERAL.STATE','');profile=data.get('GENERAL.CONNECTION','')
    if profile=='--':profile=''
    addresses=[v for k,v in data.items() if k.startswith('IP4.ADDRESS') and v not in ('','--')]
    has_ip=bool(addresses)
    has_gateway=data.get('IP4.GATEWAY','') not in ('','--')
    associated=state.split(' ',1)[0]=='100'
    connected=associated and has_ip and has_gateway
    note='Associated; waiting for IPv4/gateway' if associated and not connected else ('Connecting to upstream Wi-Fi' if state.split(' ',1)[0] in ('40','50','60','70','80','90') else 'Upstream Wi-Fi disconnected')
    return dict(client_state=state,client_connected=connected,client_note='Associated with IPv4/gateway (Internet unverified)' if connected else note,
        hotspot=profile if connected else '',attempted_hotspot=profile if not connected else '',client_uuid=data.get('GENERAL.CON-UUID',''),has_ipv4=has_ip,has_gateway=has_gateway,client_ipv4=addresses,gateway=data.get('IP4.GATEWAY',''))


# Independent broker watchdog: network coverage and client count are not faults.
# Journal messages are inspected locally; only fixed reasons/counters enter status.
import collections,logging,re
WIFI_RECOVERY=dict(state='Starting',adapter_attempts=0,service_attempts=0,usb_reset_attempts=0,last_reason='',last_error='')
WIFI_JOURNAL_REASONS={
    'client':('request-scan: could not get scan request result: Timeout',
              'call-disconnect: failed with Timeout','call-remove-network: failed with Timeout'),
    'ap':('brcmf_cyw_mgmt_tx: TX Auth frame operation is failed',)}

def gateway_health(interface,link):
    import ipaddress
    if not interface or not link.get('client_connected'):return 'unavailable'
    try:
        gateway=str(ipaddress.IPv4Address(link.get('gateway','')))
        if ipaddress.IPv4Address(gateway).is_multicast or gateway=='0.0.0.0':return 'unavailable'
        previous=subprocess.run(['ip','-j','neigh','show','to',gateway,'dev',interface],capture_output=True,text=True,timeout=2)
        # This stimulates neighbour resolution; it does not test Internet access.
        subprocess.run(['ping','-n','-I',interface,'-c','1','-W','1',gateway],capture_output=True,timeout=2)
        result=subprocess.run(['ip','-j','neigh','show','to',gateway,'dev',interface],capture_output=True,text=True,timeout=2)
        if result.returncode:return 'unavailable'
        rows=json.loads(result.stdout)
        states={s for row in rows if row.get('dst')==gateway and row.get('dev')==interface for s in row.get('state',[])}
        if 'REACHABLE' in states:return 'reachable'
        if 'FAILED' in states:return 'failed'
        if previous.returncode==0 and any(row.get('dst')==gateway and row.get('dev')==interface and 'FAILED' in row.get('state',[]) for row in json.loads(previous.stdout)):return 'failed'
    except (OSError,ValueError,TypeError,subprocess.TimeoutExpired):pass
    return 'unverified'

class WifiRecovery:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.started=clock();self.seen=set();self.events={'ap':collections.deque(maxlen=40),'client':collections.deque(maxlen=40)}
        self.auth=collections.deque(maxlen=40);self.known_networks=set();self.profile_cache={};self.client_stage=False;self.client_connected=False;self.limited=False
        self.attempts={r:collections.deque() for r in ('ap','client','usb','service')}
        self.last_attempt={r:float('-inf') for r in self.attempts}
        self.gateway_known=set();self.gateway_failures=collections.deque(maxlen=20);self.gateway_key=None;self.link_failed=False;self.gateway_suspect=False
    def observe_gateway(self,link,health):
        now=self.clock();key=(link.get('client_uuid'),link.get('gateway'))
        if key!=self.gateway_key:
            self.gateway_failures.clear();self.gateway_key=key
        self.link_failed=False;self.gateway_suspect=False
        if not link.get('client_connected') or not all(key):
            self.gateway_failures.clear();return
        if health=='reachable':
            self.gateway_known.add(key);self.gateway_failures.clear();return
        if health!='failed' or key not in self.gateway_known:
            self.gateway_failures.clear();return
        self.gateway_suspect=True
        self.gateway_failures.append(now)
        self.gateway_failures=collections.deque((t for t in self.gateway_failures if now-t<=150),maxlen=20)
        self.link_failed=len(self.gateway_failures)>=4 and now-self.gateway_failures[0]>=90

    def observe_link(self,link):
        self.client_connected=link.get('client_connected',False)
        if self.client_connected and not self.gateway_suspect:
            ident=link.get('client_uuid','')
            if ident and ident!='--':
                if ident not in self.profile_cache:self.profile_cache[ident]=nm('-g','802-11-wireless.ssid','connection','show',ident,timeout=4)
                self.known_networks.add(self.profile_cache[ident])
            self.client_stage=False;self.events['client'].clear();self.auth.clear()
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
            when=now
            try:when=float(row.get('__MONOTONIC_TIMESTAMP',now*1000000))/1000000
            except (TypeError,ValueError):continue
            if not 0<=now-when<=120:continue
            if role:self.events[role].append(when)
            if client and not self.client_connected and unit=='wpa_supplicant.service' and message.startswith(client+': ') and 'CTRL-EVENT-SSID-TEMP-DISABLED' in message and 'reason=CONN_FAILED' in message:
                match=re.search(r'\bssid="([^"\\]*)"',message)
                if match and match[1] in self.known_networks:self.auth.append((when,match[1]))
        for role in self.events:
            self.events[role]=collections.deque((t for t in self.events[role] if now-t<=120),maxlen=40)
        self.auth=collections.deque((v for v in self.auth if now-v[0]<=120),maxlen=40)
    def choose(self,ap,client):
        now=self.clock();self.limited=False
        if now-self.started<90:return None
        for role in self.attempts:
            while self.attempts[role] and now-self.attempts[role][0]>=3600:self.attempts[role].popleft()
        bad={r:len(self.events[r])>=3 for r in self.events}
        for ssid in self.known_networks:
            times=[t for t,name in self.auth if name==ssid]
            if len(times)>=3 and max(times)-min(times)>=60:bad['client']=True
        if self.client_connected:bad['client']=self.link_failed
        # Shared service restart is last resort: both adapters must have fresh
        # fault evidence, and each must already have had an isolated attempt.
        if ap and client and all(bad.values()) and all(self.attempts[r] for r in ('ap','client')) and now-self.last_attempt['service']>=900 and len(self.attempts['service'])<2:
            return 'service'
        for role,present in (('ap',ap),('client',client)):
            if not present or not bad[role]:continue
            if role=='client' and self.client_stage:
                if now-self.last_attempt['client']>=300 and now-self.last_attempt['usb']>=600 and len(self.attempts['usb'])<2:return 'usb'
            elif now-self.last_attempt[role]>=300 and len(self.attempts[role])<3:return role
            self.limited=True
        return None
    def record(self,role):
        now=self.clock();self.last_attempt[role]=now;self.attempts[role].append(now)
        if role in ('client','usb'):self.client_stage=True
        for key in self.events:self.events[key].clear()
        self.auth.clear();self.gateway_failures.clear();self.link_failed=False

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
    global CLIENT_MANAGEMENT_RESTORE
    ap=device(AP_MAC);client=device(CLIENT_MAC)
    if role=='service':
        result=subprocess.run(['/usr/bin/systemctl','restart','wpa_supplicant.service'],capture_output=True,timeout=25)
        if result.returncode:raise RuntimeError('Wi-Fi supplicant restart failed')
        # NetworkManager recreates the interfaces; AP restoration is retried by
        # the next broker tick even when initial activation is not ready yet.
        nm('connection','up',AP,timeout=18)
        return
    interface=ap if role=='ap' else client
    if not interface:raise RuntimeError('Affected Wi-Fi adapter is unavailable')
    if role=='usb':
        CLIENT_MANAGEMENT_RESTORE=True
        try:
            try:nm('device','set',interface,'managed','no')
            except Exception:logging.warning('Upstream management detach did not complete; attempting fixed-target USB reset')
            WIFI_RECOVERY['usb_reset_attempts']+=1
            request_client_usb_reset()
        finally:
            for _ in range(10):
                if restore_client_management():break
                time.sleep(.5)
            # If re-enumeration is slower, the broker guard restores by MAC
            # on a later tick; never operate on a stale interface name.
        interface=device(CLIENT_MAC)
        if not interface:raise RuntimeError('USB Wi-Fi interface has not returned; inspect/replug the Linksys')
        reconnect_client(interface)
        return
    # Preserve profiles and hardware bindings. Never create an upstream profile.
    try:nm('device','set',interface,'managed','no')
    finally:nm('device','set',interface,'managed','yes')
    if role=='ap':nm('connection','up',AP,'ifname',interface,timeout=18)
    else:reconnect_client(interface)

def reconnect_client(interface):
    saved=[p for p in profiles() if p['uuid']!=AP and p['mac']==CLIENT_MAC and p['mode']!='ap' and p['ssid'] not in forgotten()]
    saved.sort(key=lambda p:int(nm('-g','connection.autoconnect-priority','connection','show',p['uuid']) or 0),reverse=True)
    if saved:nm('connection','up',saved[0]['uuid'],'ifname',interface,timeout=18)

def wifi_health_tick(rows):
    ap=device(AP_MAC);client=device(CLIENT_MAC)
    try:
        link=client_link(client)
        gateway=gateway_health(client,link)
        WIFI_RECOVERY['gateway_health']=gateway
        wifi_recovery.observe_gateway(link,gateway)
        wifi_recovery.observe_link(link)
    except Exception:wifi_recovery.client_connected=False
    wifi_recovery.ingest(rows,ap,client)
    role=wifi_recovery.choose(ap,client)
    if wifi_recovery.client_connected and not wifi_recovery.link_failed:WIFI_RECOVERY['last_error']=''
    if role is None:
        WIFI_RECOVERY['state']='Recovery cooldown/limit active; inspect or replug Linksys if persistent' if wifi_recovery.limited else 'Monitoring (90-second startup grace)' if wifi_recovery.clock()-wifi_recovery.started<90 else 'Monitoring'
        return
    if not operation_lock.acquire(blocking=False):return
    try:
        import update_manager as updater
        with updater.locked():
            state=updater.status()
            if updater.power_pending() or (updater.STATE/'pending.json').exists() or (state.get('phase')=='Queued' and not state.get('completed_at')):
                WIFI_RECOVERY['state']='Deferred for update/power operation';return
            stalled=wifi_recovery.link_failed
            wifi_recovery.record(role)
            reason='Both adapters repeatedly failing after isolated recovery' if role=='service' else ('Repeated onboard authentication failures' if role=='ap' else ('Persistent upstream failure after adapter management recovery' if role=='usb' else 'Repeated upstream Wi-Fi control/authentication failures'))
            if stalled:reason='Persistent local gateway ARP failure on previously working upstream'
            counter='service_attempts' if role=='service' else 'adapter_attempts'
            WIFI_RECOVERY.update(state='Recovering '+role,last_reason=reason,last_error='')
            WIFI_RECOVERY[counter]+=1
            logging.warning('Wi-Fi recovery: %s',reason)
            recovery_record('attempt')
            try:recover_wifi(role)
            except Exception:
                WIFI_RECOVERY.update(state='Recovery attempted; inspect connectivity',last_error='Recovery did not complete; bounded retry applies')
                logging.warning('Wi-Fi recovery operation did not complete')
            else:WIFI_RECOVERY['state']='Recovery attempted; monitoring'
            recovery_record('attempt_finished')
    except RuntimeError:
        WIFI_RECOVERY['state']='Deferred; update lock unavailable'
    finally:operation_lock.release()

def recovery_record(event):
    import update_manager as updater
    try:
        path=updater.STATE/'wifi-recovery.json'
        history=updater.read(path,{}).get('events',[])
        if not isinstance(history,list):history=[]
        item=dict(event=event,epoch=time.time(),boot_id=updater.boot_id(),
            state=WIFI_RECOVERY['state'],reason=WIFI_RECOVERY['last_reason'],error=WIFI_RECOVERY['last_error'],
            adapter_attempts=WIFI_RECOVERY['adapter_attempts'],usb_reset_attempts=WIFI_RECOVERY['usb_reset_attempts'])
        updater.put(path,dict(events=(history+[item])[-60:]))
    except Exception:logging.warning('Wi-Fi recovery history write failed')

def wifi_health_watch():
    recovery_record('broker_started')
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
                    if CLIENT_MANAGEMENT_RESTORE:
                        import update_manager as updater
                        with updater.locked():
                            state=updater.status()
                            if not updater.power_pending() and not (updater.STATE/'pending.json').exists() and not (state.get('phase')=='Queued' and not state.get('completed_at')):restore_client_management()
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
    try:result.update(client_link(client))
    except Exception:result.update(client_connected=False,client_note='Upstream status unavailable',attempted_hotspot='')
    result.setdefault('client_ipv4',[])
    result['ap_ipv4']=[]
    if ap:
        result['ap_state']=nm('-g','GENERAL.STATE','device','show',ap)
        try:result['ap_ipv4']=[v for v in nm('-g','IP4.ADDRESS','device','show',ap).splitlines() if v not in ('','--')]
        except Exception:pass
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
    if action=='sdr_recovery':
        from sdr_health import recovery_policy
        if set(value)=={'action'}:return recovery_policy()
        return recovery_policy(value)
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
    import sys
    if sys.argv[1:]:
        if sys.argv[1:]!=['--reset-client-usb']:raise SystemExit('Unsupported broker arguments')
        print(json.dumps(reset_client_usb()));raise SystemExit(0)
    # Update timer migrations must not prevent field Wi-Fi control starting.
    try:
        from update_manager import request_support
        request_support()
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
