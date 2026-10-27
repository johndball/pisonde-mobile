#!/usr/bin/env python3
"""Root-owned, app-only stable-channel updater. Never changes radio configuration."""
import ast,base64,contextlib,fcntl,hashlib,json,logging,os,pwd,re,stat,shutil,sqlite3,subprocess,sys,tempfile,time,urllib.request,uuid
from datetime import datetime,timezone,timedelta
from pathlib import Path

APP=Path('/opt/pisonde-mobile')
STATE=Path('/var/lib/pisonde-updater')
BACKUPS=Path('/var/backups/pisonde-updater')
DATA=Path('/var/lib/pisonde-mobile/receptions.sqlite3')
HARDWARE=Path('/etc/pisonde-mobile/hardware.json')
RAW='https://raw.githubusercontent.com/johndball/pisonde-mobile/'
API='https://api.github.com/repos/johndball/pisonde-mobile/'
CHANNEL=API+'contents/updates/signed-stable.json?ref=main'
UPDATER_VERSION='0.3.18'
FILES={'mobile.py','field_features.py','field_ui.py','wifi_control.py','sdr_health.py','hardware.py','storage_maintenance.py','verify_recovery.py','update_manager.py','update_ui.py'}
TRUST=Path('/etc/pisonde-mobile/update-signing-public.pem')
ENROLL_FINGERPRINT='34e43261378f0235107cc0d1e3eb8d2bed7d44c44d995323d7646203235dbfd0'
DOMAIN=b'PiSonde-Mobile signed-stable manifest v1\n'
POWER_PENDING=Path('/run/pisonde-control/power-pending.json')
UNITS=('pisonde-wifi-control.service','pisonde-mobile.service')
DEFAULT={'scheduled':True,'reboot_if_missed':False,'immediate':False,'window_start':3,'window_end':5}
WINDOWS=tuple((start,(start+2)%24) for start in range(1,24,2))

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
def validate_settings(value):
    if not isinstance(value,dict) or set(value)!=set(DEFAULT):raise RuntimeError('Invalid update settings')
    if any(type(value[k]) is not bool for k in ('scheduled','reboot_if_missed','immediate')):
        raise RuntimeError('Update options must be true or false')
    if any(type(value[k]) is not int for k in ('window_start','window_end')) or (value['window_start'],value['window_end']) not in WINDOWS:
        raise RuntimeError('Choose a listed two-hour maintenance window')
    return value

def settings():
    value=read(STATE/'settings.json',dict(DEFAULT))
    # Read the previous on/off schema without enabling any new behavior.
    if isinstance(value,dict) and set(value)=={'automatic','window_start','window_end'}:
        value=dict(scheduled=value['automatic'],reboot_if_missed=False,immediate=False,
                   window_start=value['window_start'],window_end=value['window_end'])
    return validate_settings(value)

def window_label(cfg):return f"{cfg['window_start']:02d}00–{cfg['window_end']:02d}00 local time"
def in_window(cfg,local):
    start,end=cfg['window_start'],cfg['window_end']
    return start<=local.hour<end if start<end else local.hour>=start or local.hour<end

def last_window_end(cfg,local):
    end=local.replace(hour=cfg['window_end'],minute=0,second=0,microsecond=0)
    return end if end<=local else end-timedelta(days=1)

def boot_id():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
def boot_time(local):
    return local-timedelta(seconds=float(Path('/proc/uptime').read_text().split()[0]))

def install_reason(cfg,action,policy,local,current_boot,boot_started):
    if action=='install':return 'Manual installation'
    if cfg['immediate']:return 'Install immediately when available'
    if cfg['scheduled'] and in_window(cfg,local):
        return 'Scheduled maintenance window' if action=='run' else ''
    if action=='run' and cfg['reboot_if_missed'] and policy.get('boot_id')!=current_boot:
        seen=datetime.fromisoformat(policy['available_since'])
        # Only an update already known before a completed window qualifies.
        if seen.astimezone(boot_started.tzinfo)<last_window_end(cfg,boot_started):
            return 'Missed maintenance window; next reboot'
    return ''


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
    if not isinstance(catalog,dict) or catalog.get('schema')!=2 or catalog.get('compatibility')!='app-only-v1':raise RuntimeError('Unsupported update channel')
    version(catalog.get('version'))
    if not re.fullmatch(r'[0-9a-f]{40}',str(catalog.get('commit',''))):raise RuntimeError('Invalid source commit')
    hashes=catalog.get('files')
    if not isinstance(hashes,dict) or set(hashes)!=FILES or any(not re.fullmatch(r'[0-9a-f]{64}',str(h)) for h in hashes.values()):raise RuntimeError('Invalid update file manifest')
    return catalog

def canonical(value):
    body={k:v for k,v in value.items() if k!='signature'}
    return DOMAIN+json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')

def strict_json(data):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise RuntimeError('Duplicate manifest field')
            result[key]=value
        return result
    return json.loads(data,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(RuntimeError('Invalid manifest number')))

def trusted_key():
    if not TRUST.exists():raise RuntimeError('Signed updates require one-time public-key enrollment')
    for path in (TRUST,TRUST.parent):
        info=path.lstat()
        if info.st_uid!=0 or info.st_mode&0o022 or stat.S_ISLNK(info.st_mode):raise RuntimeError('Update trust key must be root-owned and protected from writes')
    if not stat.S_ISREG(TRUST.stat().st_mode) or TRUST.stat().st_size>4096:raise RuntimeError('Invalid public-key file')
    result=subprocess.run(['/usr/bin/openssl','pkey','-pubin','-in',str(TRUST),'-outform','DER'],capture_output=True,timeout=10)
    der=result.stdout
    if result.returncode or len(der)!=44 or not der.startswith(bytes.fromhex('302a300506032b6570032100')):
        raise RuntimeError('A trusted Ed25519 public key is required')
    return der,hashlib.sha256(der).hexdigest()

def authenticate(value,record=False):
    validate(value)
    if set(value)!={'schema','compatibility','version','commit','files','security','signature'}:raise RuntimeError('Unexpected signed manifest fields')
    security=value['security'];signature=value['signature']
    if not isinstance(security,dict) or set(security)!={'repository','channel','sequence','issued_at','expires_at'} or security['repository']!='johndball/pisonde-mobile' or security['channel']!='signed-stable':
        raise RuntimeError('Manifest is for a different repository or channel')
    if type(security['sequence']) is not int or not 1<=security['sequence']<=2**53-1:raise RuntimeError('Invalid manifest sequence')
    if not isinstance(signature,dict) or set(signature)!={'algorithm','key_id','value'} or signature['algorithm']!='ed25519':raise RuntimeError('Unsupported manifest signature')
    try:
        issued=datetime.strptime(security['issued_at'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        expires=datetime.strptime(security['expires_at'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        raw_signature=base64.b64decode(signature['value'],validate=True)
    except (ValueError,TypeError,KeyError):raise RuntimeError('Malformed signed manifest') from None
    clock=datetime.now(timezone.utc)
    if issued>clock+timedelta(minutes=5):raise RuntimeError('Manifest is from the future; check the Pi clock')
    if expires<=clock:raise RuntimeError('Signed update manifest expired; a freshly signed manifest is required')
    if not timedelta(0)<expires-issued<=timedelta(days=366):raise RuntimeError('Invalid signed manifest validity period')
    der,key_id=trusted_key()
    if signature['key_id']!=key_id or len(raw_signature)!=64:raise RuntimeError('Manifest signing key does not match the enrolled key')
    with tempfile.TemporaryDirectory(prefix='pisonde-verify-') as temp:
        root=Path(temp);(root/'key.der').write_bytes(der);(root/'manifest').write_bytes(canonical(value));(root/'signature').write_bytes(raw_signature)
        result=subprocess.run(['/usr/bin/openssl','pkeyutl','-verify','-pubin','-keyform','DER','-inkey',str(root/'key.der'),'-rawin','-in',str(root/'manifest'),'-sigfile',str(root/'signature')],capture_output=True,timeout=10)
    if result.returncode:raise RuntimeError('Update signature verification failed')
    digest=hashlib.sha256(canonical(value)).hexdigest()
    previous=read(STATE/'signature-state.json',{})
    sequence=security['sequence']
    if previous and (sequence<previous['sequence'] or sequence==previous['sequence'] and digest!=previous['digest']):
        raise RuntimeError('Signed manifest replay or sequence conflict rejected')
    if record:
        put(STATE/'signature-state.json',dict(sequence=sequence,digest=digest,key_id=key_id,verified_at=now()))
    return value

def download(url,limit):
    request=urllib.request.Request(url,headers={'User-Agent':'PiSonde-Mobile-Updater/1','Cache-Control':'no-cache'})
    with urllib.request.urlopen(request,timeout=25) as response:
        expected=API if url.startswith(API) else RAW
        if not response.geturl().startswith(expected):raise RuntimeError('Unexpected download redirect')
        data=response.read(limit+1)
    if len(data)>limit:raise RuntimeError('Update download exceeds size limit')
    return data

def catalog():
    entry=strict_json(download(CHANNEL+'&check='+str(time.time_ns()),131072))
    if entry.get('encoding')!='base64' or entry.get('path')!='updates/signed-stable.json':raise RuntimeError('Invalid channel API response')
    payload=base64.b64decode(''.join(entry['content'].split()),validate=True)
    if len(payload)>65536:raise RuntimeError('Manifest too large')
    return authenticate(strict_json(payload),record=True)

def stage(value):
    authenticate(value)
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
    if value.get('phase')=='Queued' and value.get('queued_at') and not value.get('started_at'):
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(value['queued_at'])).total_seconds()
        if age>90:value.update(phase='Update worker did not start',outcome='timeout',completed_at=now(),last_error='Queued operation exceeded 90 seconds; inspect the update worker log')
    cfg=settings()
    verified=read(STATE/'signature-state.json',{})
    value.update(signatures_required=True,signing_enrolled=TRUST.exists(),signing_key_id=verified.get('key_id',''))
    value.update(cfg,automatic=any(cfg[k] for k in ('scheduled','reboot_if_missed','immediate')),window=window_label(cfg),installed_version=app_version(),configured=(STATE/'settings.json').exists())
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
    authenticate(value)
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
        update_status(phase='Restarting application')
        services('start')
        update_status(phase='Verifying application')
        health(value['version'])
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

def power_pending():
    try:
        value=read(POWER_PENDING,{})
        return 0<=time.time()-value.get('queued_epoch',0)<90
    except Exception:return True  # A malformed power marker is not permission to update.

def run(action):
    if action not in ('run','check','install'):raise RuntimeError('Unsupported update operation')
    try:command(['systemctl','start','pisonde-update-recover.service'])
    except Exception as exc:
        current=read(STATE/'status.json',{})
        if current.get('phase')=='Queued' and current.get('operation')==action:
            update_status(phase='Update worker failed to start',outcome='failed',completed_at=now(),last_error=str(exc)[:300])
        raise
    with locked():
        if power_pending():raise RuntimeError('Device restart/shutdown is pending')
        current=read(STATE/'status.json',{})
        ident=current.get('operation_id') if current.get('phase')=='Queued' and current.get('operation')==action else uuid.uuid4().hex
        update_status(operation_id=ident,operation=action,started_at=now(),completed_at=None,outcome=None,phase='Checking',last_error='',last_result='')
        try:
            recover_running()
            cfg=settings();cached=read(STATE/'available.json')
            checked=read(STATE/'status.json',{}).get('last_check')
            fresh=checked and 0<=(datetime.now(timezone.utc)-datetime.fromisoformat(checked)).total_seconds()<6*3600
            if action=='run' and fresh and cached:
                try:value=authenticate(cached)
                except RuntimeError:
                    # A re-signed/renewed catalog may supersede an expired or stale cache.
                    value=catalog();put(STATE/'available.json',value);update_status(last_check=now())
            else:
                value=catalog();put(STATE/'available.json',value)
                update_status(last_check=now())
            installed=app_version()
            update_status(available_version=value['version'],available_commit=value['commit'],channel_blob_sha=value.get('channel_blob_sha',''))
            if version(value['version'])<=version(installed):
                update_status(phase='Up to date',outcome='up_to_date',completed_at=now());return
            cfg=settings();local=datetime.now().astimezone();current_boot=boot_id()
            policy=read(STATE/'policy.json',{})
            if policy.get('commit')!=value['commit']:
                policy=dict(commit=value['commit'],available_since=now(),boot_id=policy.get('boot_id'))
            # Persist first discovery even if download fails, without consuming a boot retry.
            put(STATE/'policy.json',policy)
            reason=install_reason(cfg,action,policy,local,current_boot,boot_time(local))
            update_status(phase='Downloading update',install_reason=reason)
            folder=STATE/'staged'/value['commit']
            if not folder.is_dir():folder=stage(value)
            if action=='install' or reason:
                apply(value,folder);update_status(outcome='installed',completed_at=now())
            else:update_status(phase='Update downloaded; ready to install',outcome='available',completed_at=now())
            if action=='run':
                policy['boot_id']=current_boot;put(STATE/'policy.json',policy)
        except Exception as exc:
            rolled_back=read(STATE/'status.json',{}).get('last_result')=='rolled_back'
            update_status(phase='Rolled back' if rolled_back else 'Update check/install failed',outcome='rolled_back' if rolled_back else 'failed',completed_at=now(),last_error=str(exc)[:300])
            raise

def broker_action(value):
    if os.geteuid()!=0:raise RuntimeError('Update control requires root broker')
    action=value.get('operation')
    expected={'action','operation','settings'} if action=='set_policy' else {'action','operation','enabled'} if action=='set_auto' else {'action','operation'}
    if set(value)!=expected:raise RuntimeError('Unexpected update parameters')
    if action=='status':return status()
    if action=='set_auto':
        if type(value['enabled']) is not bool:raise RuntimeError('Automatic updates must be true or false')
        cfg=settings();cfg.update(scheduled=value['enabled'],reboot_if_missed=False,immediate=False)
        put(STATE/'settings.json',cfg);return status()
    if action=='set_policy':
        cfg=validate_settings(value['settings'])
        with locked():put(STATE/'settings.json',cfg)
        if cfg['immediate']:
            command(['systemctl','start','--no-block','pisonde-update-run.service'])
        return status()
    if action not in ('check','install'):raise RuntimeError('Unsupported update control')
    if power_pending():raise RuntimeError('Device restart/shutdown is pending')
    if not (STATE/'settings.json').exists():raise RuntimeError('Run the one-time updater setup first')
    ident=uuid.uuid4().hex
    with locked():
        previous=read(STATE/'status.json',{})
        if previous.get('phase')=='Queued' and previous.get('queued_at'):
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(previous['queued_at'])).total_seconds()
            if age<90:raise RuntimeError('An update operation is already queued')
        update_status(operation_id=ident,operation=action,queued_at=now(),started_at=None,completed_at=None,outcome=None,phase='Queued',last_error='')
    name='pisonde-update-'+action+'.service'
    try:command(['systemctl','start','--no-block',name])
    except Exception as exc:
        update_status(phase='Update could not be queued',outcome='failed',completed_at=now(),last_error=str(exc)[:300]);raise
    return dict(queued=action,operation_id=ident)

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
Description=Check PiSonde updates and maintenance policy
[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
OnCalendar=*-*-* *:15:00
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
    if start_timer:command(['systemctl','restart','pisonde-update.timer'])
    put(STATE/'support.json',{'version':UPDATER_VERSION})

def ensure_support():
    # The old updater installs application files only. Migrate its timer once from
    # the newly restarted root broker, without requiring a separate SSH command.
    if (STATE/'settings.json').exists() and read(STATE/'support.json',{}).get('version')!=UPDATER_VERSION:
        install_support()

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

def bootstrap(selected=None):
    if os.geteuid()!=0 or not (APP/'mobile.py').is_file():raise RuntimeError('Run on an existing PiSonde Mobile installation with sudo')
    pwd.getpwnam('pisonde-mobile')
    value=authenticate(selected,record=True) if selected is not None else catalog()
    if version(value['version'])<version(UPDATER_VERSION):raise RuntimeError('Stable channel is older than this installer; retry after publication completes')
    if version(value['version'])<version(app_version()):raise RuntimeError('Refusing to downgrade this installation')
    legacy_hardware()
    legacy_upload_settings()
    os.chown(APP,0,0);APP.chmod(0o755)
    STATE.mkdir(parents=True,exist_ok=True)
    folder=stage(value)
    # Existing updater code must be backed up intact for a failed enrollment.
    if not (APP/'update_manager.py').exists():atomic(APP/'update_manager.py',(folder/'update_manager.py').read_bytes())
    install_support(start_timer=False)
    with locked():
        recover_running()
        apply(value,folder)
    command(['systemctl','start','pisonde-update.timer'])
    print('PASS: PiSonde Mobile '+value['version']+' installed. Automatic installation: '+str(status()['automatic'])+'; window '+window_label(settings())+'.')
    print('Dashboard: http://192.168.73.1/updates')

def enroll(value,public_path):
    if os.geteuid()!=0:raise RuntimeError('Public-key enrollment requires root')
    raw=Path(public_path).read_bytes()
    if len(raw)>4096 or not raw.startswith(b'-----BEGIN PUBLIC KEY-----'):raise RuntimeError('Only a public key may be enrolled')
    result=subprocess.run(['/usr/bin/openssl','pkey','-pubin','-in',str(public_path),'-outform','DER'],capture_output=True,timeout=10)
    if result.returncode or hashlib.sha256(result.stdout).hexdigest()!=ENROLL_FINGERPRINT:raise RuntimeError('Enrollment key differs from the independently verified publisher fingerprint')
    TRUST.parent.mkdir(parents=True,exist_ok=True)
    info=TRUST.parent.lstat()
    if info.st_uid!=0 or info.st_mode&0o022 or stat.S_ISLNK(info.st_mode):raise RuntimeError('Unsafe public-key directory')
    if TRUST.exists() or TRUST.is_symlink():
        if trusted_key()[1]!=ENROLL_FINGERPRINT:raise RuntimeError('Refusing to replace an already enrolled key')
    else:atomic(TRUST,raw)
    # A failed transition must not silently resume an unsigned automatic updater.
    command(['systemctl','stop','pisonde-update.timer'])
    try:bootstrap(value)
    except BaseException:
        command(['systemctl','stop','pisonde-update.timer'])
        raise
    print('PASS: signed updates enrolled; unsigned fallback is disabled.')

def main():
    if os.geteuid()!=0:raise RuntimeError('Run update worker as root')
    action=sys.argv[1] if len(sys.argv) in (2,3) else ''
    if action=='bootstrap':return bootstrap(read(Path(sys.argv[2])) if len(sys.argv)==3 else None)
    if len(sys.argv)!=2:raise RuntimeError('Invalid updater arguments')
    if action=='recover':
        with locked():return recover()
    return run(action)
if __name__=='__main__':
    try:main()
    except Exception as exc:
        logging.error('PiSonde update failed: %s',exc);sys.exit(1)
