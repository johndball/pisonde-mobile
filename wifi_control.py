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

def status():
    client=device(CLIENT_MAC);ap=device(AP_MAC)
    result=dict(ap_mac=AP_MAC,client_mac=CLIENT_MAC,ap_interface=ap,client_interface=client,onboard_present=bool(ap),usb_present=bool(client),role_layout=ROLE_LAYOUT,ap_present=bool(ap),hotspot='',client_state='',ap_state='')
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

def handle(value):
    action=value.get('action')
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
    ensure()
    Path(SOCKET).unlink(missing_ok=True)
    server=ThreadingUnixStreamServer(SOCKET,Handler)
    os.chmod(SOCKET,0o660)
    import grp
    os.chown(SOCKET,0,grp.getgrnam('pisonde-mobile').gr_gid)
    server.serve_forever()
