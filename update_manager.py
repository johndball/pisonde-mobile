#!/usr/bin/env python3
"""Root-owned, app-only stable-channel updater. Never changes radio configuration."""
import ast,contextlib,fcntl,hashlib,json,logging,os,pwd,re,shutil,sqlite3,subprocess,sys,tempfile,time,urllib.request,uuid
from datetime import datetime,timezone
from pathlib import Path

APP=Path('/opt/pisonde-mobile')
STATE=Path('/var/lib/pisonde-updater')
BACKUPS=Path('/var/backups/pisonde-updater')
DATA=Path('/var/lib/pisonde-mobile/receptions.sqlite3')
HARDWARE=Path('/etc/pisonde-mobile/hardware.json')
RAW='https://raw.githubusercontent.com/johndball/pisonde-mobile/'
CHANNEL=RAW+'main/updates/stable.json'
FILES={'mobile.py','field_features.py','field_ui.py','wifi_control.py','sdr_health.py','hardware.py','storage_maintenance.py','verify_recovery.py','update_manager.py','update_ui.py'}
UNITS=('pisonde-wifi-control.service','pisonde-mobile.service')
DEFAULT={'automatic':True,'window_start':3,'window_end':5}

def now():return datetime.now(timezone.utc).isoformat()
def atomic(path,data,mode=0o644):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.chmod(temp,mode);os.replace(temp,path)
        directory=os.open(path.parent,os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(temp):os.unlink(temp)
def put(path,value):atomic(path,(json.dumps(value,indent=2)+'\n').encode())
def read(path,default=None):
    if not path.exists():return default
    return json.loads(path.read_text())
def settings():
    value=read(STATE/'settings.json',DEFAULT)
    if set(value)!=set(DEFAULT) or type(value['automatic']) is not bool or any(type(value[k]) is not int or not 0<=value[k]<=23 for k in ('window_start','window_end')) or value['window_start']>=value['window_end']:
        raise RuntimeError('Invalid update settings')
    return value

def version(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d+\.\d+\.\d+',value):raise RuntimeError('Invalid stable version')
    return tuple(map(int,value.split('.')))
def app_version(path=None):
    tree=ast.parse((path or APP/'mobile.py').read_text())
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(n,ast.Name) and n.id=='VERSION' for n in node.targets):
            value=ast.literal_eval(node.value);version(value);return value
    raise RuntimeError('Application version missing')
def validate(catalog):
    if not isinstance(catalog,dict) or catalog.get('schema')!=1 or catalog.get('compatibility')!='app-only-v1':raise RuntimeError('Unsupported update channel')
    version(catalog.get('version'))
    if not re.fullmatch(r'[0-9a-f]{40}',str(catalog.get('commit',''))):raise RuntimeError('Invalid source commit')
    hashes=catalog.get('files')
    if not isinstance(hashes,dict) or set(hashes)!=FILES or any(not re.fullmatch(r'[0-9a-f]{64}',str(h)) for h in hashes.values()):raise RuntimeError('Invalid update file manifest')
    return catalog

def download(url,limit):
    request=urllib.request.Request(url,headers={'User-Agent':'PiSonde-Mobile-Updater/1','Cache-Control':'no-cache'})
    with urllib.request.urlopen(request,timeout=25) as response:
        if not response.geturl().startswith(RAW):raise RuntimeError('Unexpected download redirect')
        data=response.read(limit+1)
    if len(data)>limit:raise RuntimeError('Update download exceeds size limit')
    return data

def catalog():return validate(json.loads(download(CHANNEL+'?check='+str(time.time_ns()),65536)))
def stage(value):
    validate(value)
    root=STATE/'staged';root.mkdir(parents=True,exist_ok=True)
    target=root/value['commit']
    with tempfile.TemporaryDirectory(prefix='.download-',dir=root) as temp:
        temp=Path(temp)
        for name,digest in value['files'].items():
            data=download(RAW+value['commit']+'/'+name,262144)
            if hashlib.sha256(data).hexdigest()!=digest:raise RuntimeError('Checksum mismatch: '+name)
            compile(data,name,'exec');(temp/name).write_bytes(data)
        if app_version(temp/'mobile.py')!=value['version']:raise RuntimeError('Manifest/source version mismatch')
        put(temp/'manifest.json',value)
        if target.exists():shutil.rmtree(target)
        os.replace(temp,target)
        # TemporaryDirectory cleanup tolerates the renamed path.
    return target

def command(args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=45)
    if result.returncode:raise RuntimeError('Update command failed: '+args[0])
    return result.stdout.strip()
def services(action):
    names=list(reversed(UNITS)) if action=='stop' else list(UNITS)
    for name in names:command(['systemctl',action,name])
def health(expected,seconds=60):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            if all(command(['systemctl','is-active',unit])=='active' for unit in UNITS):
                with urllib.request.urlopen('http://127.0.0.1/api/status',timeout=3) as response:state=json.load(response)
                if state.get('version')==expected:
                    with sqlite3.connect('file:'+str(DATA)+'?mode=ro',uri=True,timeout=5) as db:
                        if db.execute('PRAGMA quick_check').fetchone()[0]=='ok':return
        except Exception:pass
        time.sleep(1)
    raise RuntimeError('New application failed service/API/database health checks')
def update_status(**fields):
    value=read(STATE/'status.json',{});value.update(fields);put(STATE/'status.json',value)
def status():
    value=read(STATE/'status.json',{})
    value.update(automatic=settings()['automatic'],window='03:00–05:00 local time',installed_version=app_version(),configured=(STATE/'settings.json').exists())
    return value

@contextlib.contextmanager
def locked():
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'operation.lock').open('a') as stream:
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Another update operation is running')
        yield

def backup_database(path):
    if not DATA.is_file():raise RuntimeError('Reception database missing; update refused')
    with sqlite3.connect(DATA,timeout=15) as source,sqlite3.connect(path) as target:
        source.backup(target)
        if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('Pre-update database backup failed validation')
    with path.open('rb') as stream:os.fsync(stream.fileno())
def restore(transaction):
    folder=Path(transaction['backup'])
    if folder.parent!=BACKUPS or not folder.is_dir():raise RuntimeError('Invalid recovery backup')
    for name in FILES:
        old=folder/name
        if old.is_file():atomic(APP/name,old.read_bytes())
        elif name in transaction['absent']:(APP/name).unlink(missing_ok=True)
    (STATE/'pending.json').unlink(missing_ok=True)

def recover():
    pending=read(STATE/'pending.json')
    if pending:
        restore(pending)
        update_status(phase='Recovered previous application after interrupted update',last_error='',recovered_at=now())

def apply(value,folder):
    validate(value)
    # Check staged bytes again immediately before stopping reception.
    for name,digest in value['files'].items():
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Staged content changed')
    if app_version(folder/'mobile.py')!=value['version']:raise RuntimeError('Staged version changed')
    BACKUPS.mkdir(parents=True,exist_ok=True);BACKUPS.chmod(0o700)
    backup=BACKUPS/(str(time.time_ns())+'-'+uuid.uuid4().hex);backup.mkdir(mode=0o700)
    required=DATA.stat().st_size*2+sum((folder/n).stat().st_size for n in FILES)*3+64*1024*1024
    if shutil.disk_usage(BACKUPS).free<required:raise RuntimeError('Insufficient space for safe update backup')
    previous=app_version();absent=[]
    for name in FILES:
        path=APP/name
        if path.is_file():atomic(backup/name,path.read_bytes(),0o600)
        else:absent.append(name)
    backup_database(backup/'receptions.sqlite3')
    for directory in (backup,BACKUPS):
        fd=os.open(directory,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    transaction=dict(backup=str(backup),absent=absent,previous_version=previous,target_version=value['version'])
    put(STATE/'pending.json',transaction)
    update_status(phase='Installing',last_error='')
    try:
        services('stop')
        for name in sorted(FILES):atomic(APP/name,(folder/name).read_bytes())
        services('start');health(value['version'])
    except BaseException:
        try:services('stop')
        except Exception:pass
        restore(transaction)
        services('start')
        update_status(phase='Rolled back',last_error='Update failed health checks; previous application restored',last_result='rolled_back')
        raise
    (STATE/'pending.json').unlink()
    update_status(phase='Up to date',installed_commit=value['commit'],last_success=now(),last_result='installed',last_error='')
    # Retain a bounded number of app/database rollback snapshots.
    snapshots=sorted((p for p in BACKUPS.iterdir() if p.is_dir()),key=lambda p:p.name,reverse=True)
    for old in snapshots[3:]:shutil.rmtree(old)

def recover_running():
    if (STATE/'pending.json').exists():
        services('stop');recover();services('start')

def run(action):
    if action not in ('run','check','install'):raise RuntimeError('Unsupported update operation')
    command(['systemctl','start','pisonde-update-recover.service'])
    with locked():
        recover_running()
        update_status(phase='Checking',last_error='')
        try:
            value=catalog();current=app_version()
            update_status(last_check=now(),available_version=value['version'],available_commit=value['commit'])
            if version(value['version'])<=version(current):
                update_status(phase='Up to date');return
            folder=stage(value)
            cfg=settings();hour=datetime.now().hour
            automatic=action=='run' and cfg['automatic'] and cfg['window_start']<=hour<cfg['window_end']
            if action=='install' or automatic:apply(value,folder)
            else:update_status(phase='Update downloaded; ready to install')
        except Exception as exc:
            if read(STATE/'status.json',{}).get('last_result')!='rolled_back':update_status(phase='Update check/install failed',last_error=str(exc)[:300])
            raise

def broker_action(value):
    if os.geteuid()!=0:raise RuntimeError('Update control requires root broker')
    action=value.get('operation')
    expected={'action','operation','enabled'} if action=='set_auto' else {'action','operation'}
    if set(value)!=expected:raise RuntimeError('Unexpected update parameters')
    if action=='status':return status()
    if action=='set_auto':
        if type(value['enabled']) is not bool:raise RuntimeError('Automatic updates must be true or false')
        cfg=settings();cfg['automatic']=value['enabled'];put(STATE/'settings.json',cfg);return status()
    if action not in ('check','install'):raise RuntimeError('Unsupported update control')
    if not (STATE/'settings.json').exists():raise RuntimeError('Run the one-time updater setup first')
    name='pisonde-update-'+action+'.service'
    command(['systemctl','start','--no-block',name])
    return dict(queued=action)

RECOVER_UNIT='''[Unit]
Description=Recover interrupted PiSonde application update
Before=pisonde-mobile.service pisonde-wifi-control.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/pisonde-mobile/update_manager.py recover
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
'''
def install_support(units=None,start_timer=True):
    if os.geteuid()!=0:raise RuntimeError('Run updater setup as root')
    STATE.mkdir(parents=True,exist_ok=True);STATE.chmod(0o755)
    if not (STATE/'settings.json').exists():put(STATE/'settings.json',DEFAULT)
    units=units or Path('/etc/systemd/system')
    atomic(units/'pisonde-update-recover.service',RECOVER_UNIT.encode())
    for action in ('run','check','install'):
        unit='''[Unit]
Description=PiSonde stable-channel application update
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=root
WorkingDirectory=/
ExecStart=/usr/bin/python3 /opt/pisonde-mobile/update_manager.py ACTION
TimeoutStartSec=15min
UMask=0022
'''.replace('ACTION',action)
        atomic(units/('pisonde-update-'+action+'.service'),unit.encode())
    timer='''[Unit]
Description=Check PiSonde stable updates every six hours
[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
OnCalendar=*-*-* 03:15:00
Persistent=true
RandomizedDelaySec=5min
Unit=pisonde-update-run.service
[Install]
WantedBy=timers.target
'''
    atomic(units/'pisonde-update.timer',timer.encode())
    for unit in UNITS:
        atomic(units/(unit+'.d')/'updates.conf',b'[Unit]\nRequires=pisonde-update-recover.service\nAfter=pisonde-update-recover.service\n')
    command(['systemctl','daemon-reload'])
    command(['systemctl','enable','pisonde-update-recover.service','pisonde-update.timer'])
    command(['systemctl','start','pisonde-update-recover.service'])
    if start_timer:command(['systemctl','start','pisonde-update.timer'])

def legacy_hardware():
    path=HARDWARE
    if path.exists():return
    def constants(file):
        result={}
        for node in ast.parse(file.read_text()).body:
            if isinstance(node,ast.Assign):
                try:value=ast.literal_eval(node.value)
                except (ValueError,TypeError):continue
                for target in node.targets:
                    if isinstance(target,ast.Name):result[target.id]=value
        return result
    wifi=constants(APP/'wifi_control.py');mobile=constants(APP/'mobile.py')
    data=dict(onboard_mac=wifi.get('ONBOARD'),client_mac=wifi.get('USB'),ap_uuid=wifi.get('AP'),sdr_serial=mobile.get('DEVICE'))
    if not all(isinstance(x,str) and x for x in data.values()):raise RuntimeError('Cannot safely migrate hardware settings; nothing applied')
    if not all(re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}',data[k]) for k in ('onboard_mac','client_mac')) or data['onboard_mac'].upper()==data['client_mac'].upper():raise RuntimeError('Invalid legacy adapters')
    uuid.UUID(data['ap_uuid'])
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',data['sdr_serial']):raise RuntimeError('Invalid legacy SDR serial')
    put(path,data)

def legacy_upload_settings():
    path=DATA.parent/'uploads.json'
    if path.exists():return
    tree=ast.parse((APP/'field_features.py').read_text())
    for node in tree.body:
        if not isinstance(node,ast.Assign) or not any(isinstance(n,ast.Name) and n.id=='DEFAULT' for n in node.targets):continue
        value=node.value
        if not isinstance(value,ast.Call) or not isinstance(value.func,ast.Name) or value.func.id!='dict' or value.args:break
        try:settings_value={item.arg:ast.literal_eval(item.value) for item in value.keywords if item.arg}
        except (ValueError,TypeError):break
        if type(settings_value.get('enabled')) is not bool or not re.fullmatch(r'[A-Z0-9]{1,6}(?:-\d{1,2})?',str(settings_value.get('callsign',''))):break
        atomic(path,(json.dumps(settings_value,indent=2)+'\n').encode(),0o600)
        account=pwd.getpwnam('pisonde-mobile');os.chown(path,account.pw_uid,account.pw_gid)
        return
    raise RuntimeError('Cannot preserve upload defaults; save upload settings in the current dashboard first')

def bootstrap():
    if os.geteuid()!=0 or not (APP/'mobile.py').is_file():raise RuntimeError('Run on an existing PiSonde Mobile installation with sudo')
    pwd.getpwnam('pisonde-mobile')
    value=catalog()
    if version(value['version'])<version(app_version()):raise RuntimeError('Refusing to downgrade this installation')
    legacy_hardware()
    legacy_upload_settings()
    os.chown(APP,0,0);APP.chmod(0o755)
    STATE.mkdir(parents=True,exist_ok=True)
    folder=stage(value)
    atomic(APP/'update_manager.py',(folder/'update_manager.py').read_bytes())
    install_support(start_timer=False)
    with locked():
        recover_running()
        # Supply recovery executable before creating a pending transaction.
        # The old receiver does not import this new module.
        atomic(APP/'update_manager.py',(folder/'update_manager.py').read_bytes())
        apply(value,folder)
    command(['systemctl','start','pisonde-update.timer'])
    print('PASS: stable-channel updater installed. Automatic installation: '+str(settings()['automatic'])+'; window 03:00–05:00 local time.')
    print('Dashboard: http://192.168.73.1/updates')

def main():
    if os.geteuid()!=0:raise RuntimeError('Run update worker as root')
    action=sys.argv[1] if len(sys.argv)==2 else ''
    if action=='bootstrap':return bootstrap()
    if action=='recover':
        with locked():return recover()
    return run(action)
if __name__=='__main__':
    try:main()
    except Exception as exc:
        logging.error('PiSonde update failed: %s',exc);sys.exit(1)
