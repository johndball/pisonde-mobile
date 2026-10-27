#!/usr/bin/env python3
"""One-time trusted enrollment. Verify this installer's hash before running as root."""
import base64,hashlib,importlib.util,json,os,re,subprocess,sys,tempfile,time,urllib.request
from datetime import datetime,timedelta,timezone
from pathlib import Path
PUBLIC=b'''-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEALs2kOlOUXQlx9/231xYQ7qolodt8zD+5YRF4XKrft7s=
-----END PUBLIC KEY-----
'''
FINGERPRINT='34e43261378f0235107cc0d1e3eb8d2bed7d44c44d995323d7646203235dbfd0'
RAW='https://raw.githubusercontent.com/johndball/pisonde-mobile/'
API='https://api.github.com/repos/johndball/pisonde-mobile/'
DOMAIN=b'PiSonde-Mobile signed-stable manifest v1\n'
FILES={'mobile.py','field_features.py','field_ui.py','wifi_control.py','sdr_health.py','hardware.py','storage_maintenance.py','verify_recovery.py','update_manager.py','update_ui.py'}

def strict_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise RuntimeError('Duplicate manifest field')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(RuntimeError('Invalid JSON number')))

def fetch(url,limit):
    request=urllib.request.Request(url,headers={'User-Agent':'PiSonde-Signed-Enrollment/1','Cache-Control':'no-cache'})
    with urllib.request.urlopen(request,timeout=30) as response:
        expected=API if url.startswith(API) else RAW
        if not response.geturl().startswith(expected):raise RuntimeError('Unexpected download redirect')
        data=response.read(limit+1)
    if len(data)>limit:raise RuntimeError('Enrollment download exceeds size limit')
    return data

def verify(value,public_path):
    if not isinstance(value,dict) or set(value)!={'schema','compatibility','version','commit','files','security','signature'} or type(value['schema']) is not int or value['schema']!=2 or value['compatibility']!='app-only-v1':raise RuntimeError('Expected a signed schema-2 manifest')
    if not isinstance(value['version'],str) or not re.fullmatch(r'\d+\.\d+\.\d+',value['version']) or tuple(map(int,value['version'].split('.')))<(0,3,15):raise RuntimeError('Expected signed release 0.3.15 or later')
    if not re.fullmatch('[0-9a-f]{40}',str(value['commit'])) or not isinstance(value['files'],dict) or set(value['files'])!=FILES or any(not re.fullmatch('[0-9a-f]{64}',str(h)) for h in value['files'].values()):raise RuntimeError('Invalid signed source manifest')
    security=value['security'];signature=value['signature']
    if not isinstance(security,dict) or set(security)!={'repository','channel','sequence','issued_at','expires_at'} or security['repository']!='johndball/pisonde-mobile' or security['channel']!='signed-stable' or type(security['sequence']) is not int or not 1<=security['sequence']<=2**53-1:raise RuntimeError('Invalid signed channel metadata')
    if not isinstance(signature,dict) or set(signature)!={'algorithm','key_id','value'} or signature['algorithm']!='ed25519' or signature['key_id']!=FINGERPRINT:raise RuntimeError('Manifest is not signed by the pinned publisher key')
    issued=datetime.strptime(security['issued_at'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    expires=datetime.strptime(security['expires_at'],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc);clock=datetime.now(timezone.utc)
    if issued>clock+timedelta(minutes=5) or expires<=clock or not timedelta(0)<expires-issued<=timedelta(days=366):raise RuntimeError('Manifest expired or clock/validity is incorrect')
    result=subprocess.run(['/usr/bin/openssl','pkey','-pubin','-in',str(public_path),'-outform','DER'],capture_output=True,timeout=10)
    if result.returncode or len(result.stdout)!=44 or not result.stdout.startswith(bytes.fromhex('302a300506032b6570032100')) or hashlib.sha256(result.stdout).hexdigest()!=FINGERPRINT:raise RuntimeError('Pinned public-key fingerprint mismatch')
    raw_signature=base64.b64decode(signature['value'],validate=True)
    if len(raw_signature)!=64:raise RuntimeError('Invalid signature size')
    body=DOMAIN+json.dumps({k:v for k,v in value.items() if k!='signature'},sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
    with tempfile.TemporaryDirectory(prefix='pisonde-enroll-verify-') as temp:
        root=Path(temp);(root/'manifest').write_bytes(body);(root/'signature').write_bytes(raw_signature)
        result=subprocess.run(['/usr/bin/openssl','pkeyutl','-verify','-pubin','-inkey',str(public_path),'-rawin','-in',str(root/'manifest'),'-sigfile',str(root/'signature')],capture_output=True,timeout=10)
    if result.returncode:raise RuntimeError('Publisher signature verification failed; nothing installed')
    return value

def prepare(root):
    public=root/'publisher.pem';public.write_bytes(PUBLIC)
    entry=strict_json(fetch(API+'contents/updates/signed-stable.json?ref=main&enroll='+str(time.time_ns()),131072))
    if entry.get('encoding')!='base64' or entry.get('path')!='updates/signed-stable.json':raise RuntimeError('Signed channel not published yet or API response invalid')
    raw=base64.b64decode(''.join(entry['content'].split()),validate=True)
    if len(raw)>65536:raise RuntimeError('Manifest too large')
    value=verify(strict_json(raw),public)
    source=fetch(RAW+value['commit']+'/update_manager.py',262144)
    if hashlib.sha256(source).hexdigest()!=value['files']['update_manager.py']:raise RuntimeError('Signed updater checksum mismatch; nothing executed')
    compile(source,'update_manager.py','exec')
    path=root/'verified_updater.py';path.write_bytes(source)
    return value,public,path

def main():
    if os.geteuid()!=0:raise RuntimeError('Run the verified enrollment installer with sudo python3')
    if not Path('/opt/pisonde-mobile/mobile.py').is_file():raise RuntimeError('Configure PiSonde Mobile before enrollment')
    with tempfile.TemporaryDirectory(prefix='pisonde-enrollment-') as temp:
        value,public,path=prepare(Path(temp))
        print('Verified signed release:',value['version']);print('Publisher fingerprint:',FINGERPRINT,flush=True)
        # No downloaded Python is imported or executed before the signature and hash checks above.
        spec=importlib.util.spec_from_file_location('verified_pisonde_updater',path)
        updater=importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)
        updater.enroll(value,public)

if __name__=='__main__':
    try:main()
    except Exception as exc:print('ENROLLMENT FAILED:',str(exc),file=sys.stderr);sys.exit(1)
