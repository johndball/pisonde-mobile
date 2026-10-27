#!/usr/bin/env python3
"""One-time local setup. Use Ethernet/local console; Wi-Fi roles will change."""
import getpass,json,os,re,subprocess,sys,time,uuid
from pathlib import Path

ROOT=Path('/etc/pisonde-mobile')
CONFIG=ROOT/'hardware.json'
STATE=ROOT/'setup-state.json'
AP_SSID='pisondemobile'

def run(args,timeout=45):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('Setup operation failed: '+args[0]+' (no credentials logged)')
    return p.stdout.strip()

def nm(*args):return run(['nmcli','--wait','25','--escape','no',*args])
def get(ident,key):return nm('-g',key,'connection','show',ident)

def adapters():
    found={}
    for entry in sorted(Path('/sys/class/net').iterdir()):
        if not (entry/'wireless').exists():continue
        name=entry.name
        permanent=run(['ethtool','-P',name]).split()[-1].upper()
        if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}',permanent) or permanent=='00:00:00:00:00:00':
            raise RuntimeError('Cannot determine permanent adapter address')
        usb='/usb' in str((entry/'device').resolve())
        found[name]=dict(mac=permanent,usb=usb)
    return found

def choose(found,usb,label):
    candidates=[n for n,v in found.items() if v['usb']==usb]
    if len(candidates)!=1:raise RuntimeError('Setup requires exactly one onboard Wi-Fi and one USB Wi-Fi adapter')
    print(label+': '+candidates[0])
    return candidates[0]

def sdr_serial():
    found=[]
    for entry in Path('/sys/bus/usb/devices').iterdir():
        try:
            if (entry/'idVendor').read_text().strip()=='0bda' and (entry/'idProduct').read_text().strip()=='2838':
                found.append((entry/'serial').read_text().strip())
        except OSError:continue
    if len(found)!=1 or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',found[0]):
        raise RuntimeError('Connect exactly one RTL-SDR Blog V4 with a readable serial')
    return found[0]

def write_json(path,value,mode):
    path.write_text(json.dumps(value,indent=2)+'\n');path.chmod(mode)

def rollback(state):
    # Restore only properties changed by this worker, then the previous uplink.
    for row in state['profiles']:
        try:
            nm('connection','modify',row['uuid'],'connection.interface-name',row['interface'],
               '802-11-wireless.mac-address',row['mac'],'802-11-wireless.cloned-mac-address',row['cloned'])
        except Exception:print('A prior profile could not be restored; use Ethernet/local console.')
    try:nm('connection','delete',state['hardware']['ap_uuid'])
    except Exception:pass
    for ident in state['active']:
        try:nm('connection','up',ident)
        except Exception:pass
    CONFIG.unlink(missing_ok=True)

def activate():
    state=json.loads(STATE.read_text());data=state['hardware']
    try:
        for row in state['profiles']:
            nm('connection','modify',row['uuid'],'connection.interface-name','',
               '802-11-wireless.mac-address',data['client_mac'],'802-11-wireless.cloned-mac-address','permanent')
        nm('connection','modify',data['ap_uuid'],'connection.autoconnect','yes')
        nm('connection','up',data['ap_uuid'],'ifname',state['ap_interface'])
    except Exception:
        rollback(state)
        raise RuntimeError('AP activation failed; prior Wi-Fi bindings restored where possible')
    # Lack of Internet is allowed: keep the local access point available.
    for ident in state['active']:
        try:nm('connection','up',ident,'ifname',state['client_interface'])
        except Exception:print('Previous upstream unavailable; add a hotspot from the local Wi-Fi page.')
    from update_manager import install_support
    install_support()
    run(['systemctl','enable','--now','pisonde-wifi-control.service','pisonde-mobile.service','pisonde-history-backup.timer'])
    print('Configured: onboard AP at http://192.168.73.1; USB Wi-Fi upstream. Uploads start disabled.')

def main():
    if os.geteuid()!=0:raise RuntimeError('Run with sudo')
    if sys.argv[1:]==['--activate']:return activate()
    if sys.argv[1:]:raise RuntimeError('No arguments required')
    if CONFIG.exists():raise RuntimeError('Already configured; use the dashboard for hotspot and upload settings')
    found=adapters();ap=choose(found,False,'Onboard access point');client=choose(found,True,'USB upstream')
    serial=sdr_serial()
    # Check the selected onboard radio can provide an access point.
    phy=Path('/sys/class/net')/ap/'phy80211'
    info=run(['iw','phy',phy.resolve().name,'info'])
    if not re.search(r'^\s*\* AP\s*$',info,re.M):raise RuntimeError('Onboard Wi-Fi does not report AP support')
    print('This creates pisondemobile on onboard Wi-Fi. SSH over Wi-Fi will disconnect.')
    password=getpass.getpass('New AP password (8–63 ASCII characters): ')
    if not 8<=len(password)<=63 or not password.isascii() or any(ord(c)<32 or ord(c)>126 for c in password):
        raise RuntimeError('Use 8–63 printable ASCII characters')
    if getpass.getpass('Repeat password: ')!=password:raise RuntimeError('Passwords differ')
    rows=[];active=set(nm('-g','UUID','connection','show','--active').splitlines())
    for ident in nm('-g','UUID','connection','show').splitlines():
        if get(ident,'connection.type')!='802-11-wireless':continue
        if get(ident,'802-11-wireless.mode')=='ap':raise RuntimeError('Existing AP found; use a fresh OS image')
        row={'uuid':ident}
        for field,key in [('interface','connection.interface-name'),('mac','802-11-wireless.mac-address'),('cloned','802-11-wireless.cloned-mac-address')]:
            value=get(ident,key);row[field]='' if value=='--' else value
        rows.append(row)
    data=dict(onboard_mac=found[ap]['mac'],client_mac=found[client]['mac'],ap_uuid=str(uuid.uuid4()),sdr_serial=serial)
    ident=data['ap_uuid']
    try:
        nm('connection','add','type','wifi','ifname','*','con-name','PiSonde-Mobile','connection.uuid',ident,'ssid',AP_SSID,
           'connection.autoconnect','no','802-11-wireless.mac-address',data['onboard_mac'])
        nm('connection','modify',ident,'802-11-wireless.mode','ap','802-11-wireless.band','bg','802-11-wireless.channel','1',
           '802-11-wireless.cloned-mac-address','permanent','802-11-wireless.powersave','2',
           'wifi-sec.key-mgmt','wpa-psk','wifi-sec.proto','rsn','wifi-sec.pairwise','ccmp','wifi-sec.group','ccmp','wifi-sec.psk',password,
           'ipv4.method','shared','ipv4.addresses','192.168.73.1/24','ipv4.never-default','yes','ipv6.method','disabled')
        write_json(STATE,dict(hardware=data,profiles=rows,active=[r['uuid'] for r in rows if r['uuid'] in active],ap_interface=ap,client_interface=client),0o600)
        write_json(CONFIG,data,0o644)
        unit='pisonde-first-setup-'+str(time.time_ns())
        run(['systemd-run','--unit='+unit,'--on-active=3s','/usr/bin/python3','/opt/pisonde-mobile/configure.py','--activate'])
    except Exception:
        CONFIG.unlink(missing_ok=True)
        try:nm('connection','delete',ident)
        except Exception:pass
        raise
    print('Activation scheduled. Connect to pisondemobile in about a minute, then http://192.168.73.1')
    print('Setup log: journalctl -u '+unit+' --no-pager')
if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Setup stopped: '+str(exc),file=sys.stderr);sys.exit(1)
