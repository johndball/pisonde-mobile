#!/usr/bin/env python3
"""Fresh-card signed-release checks. Existing devices use their enrolled updater."""
import argparse,hashlib,importlib.util,json,os,stat,time
from pathlib import Path
import update_manager as u

_spec=importlib.util.spec_from_file_location('fresh_enrollment_verifier',Path(__file__).with_name('enroll-signed-updates.py'))
verifier=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(verifier)

def verify_source(source,value):
    source=Path(source)
    verifier.verify(value,source/'updates/signing-public.pem')
    if u.app_version(source/'mobile.py')!=value['version']:raise RuntimeError('Source version differs from signed release')
    verify_files(source,value)
    return value

def verify_files(folder,value):
    for name,digest in value['files'].items():
        path=Path(folder)/name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise RuntimeError('Source differs from signed release: '+name)

def preflight(source,state):
    source=Path(source);version=u.app_version(source/'mobile.py');u.version(version)
    relative='updates/releases/'+version+'.json'
    entry=verifier.strict_json(verifier.fetch(verifier.API+'contents/'+relative+'?ref=main&install='+str(time.time_ns()),131072))
    if entry.get('encoding')!='base64' or entry.get('path')!=relative:raise RuntimeError('Signed metadata for this release is not published')
    import base64
    raw=base64.b64decode(''.join(entry['content'].split()),validate=True)
    if len(raw)>65536:raise RuntimeError('Signed manifest too large')
    value=verify_source(source,verifier.strict_json(raw))
    u.atomic(Path(state),raw,0o600)
    print('PASS: fresh-install source matches signed release '+value['version'])
    return value

def secure_directory(path):
    path=Path(path)
    if path.exists() or path.is_symlink():
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):raise RuntimeError('Configuration directory must be a real directory')
    else:path.mkdir(parents=True,mode=0o755)
    # Normalize the directory only. Application data and individual config files
    # keep their existing owners; the updater key's parent cannot be service-owned.
    os.chown(path,0,0);path.chmod(0o755)

def enroll_fresh(source,state):
    if u.HARDWARE.exists() or u.DATA.exists():raise RuntimeError('Already configured: use the GUI updater, not fresh-install enrollment')
    source=Path(source);value=verify_source(source,verifier.strict_json(Path(state).read_bytes()))
    verify_files(u.APP,value)
    secure_directory(u.TRUST.parent)
    if u.TRUST.exists() or u.TRUST.is_symlink():
        if u.trusted_key()[1]!=verifier.FINGERPRINT:raise RuntimeError('Existing trust key differs from the publisher')
    else:u.atomic(u.TRUST,(source/'updates/signing-public.pem').read_bytes())
    u.authenticate(value,record=True)
    u.put(u.STATE/'available.json',value)
    u.update_status(phase='Signed release installed; configure hardware',last_check=u.now(),available_version=value['version'],available_commit=value['commit'],installed_commit=value['commit'],last_error='')
    print('PASS: root-owned public key enrolled; signed updates ready for hardware setup.')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('preflight','enroll'));parser.add_argument('source',type=Path);parser.add_argument('state',type=Path)
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('Run fresh-install checks as root')
    if args.action=='preflight':preflight(args.source,args.state)
    else:enroll_fresh(args.source,args.state)

if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit('FRESH INSTALL STOPPED: '+str(exc))
