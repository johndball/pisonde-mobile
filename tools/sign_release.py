#!/usr/bin/env python3
"""Offline publisher tool. Private keys never belong in this repository or on a Pi."""
import argparse,base64,getpass,hashlib,json,os,re,sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey,Ed25519PublicKey

DOMAIN=b'PiSonde-Mobile signed-stable manifest v1\n'
REPOSITORY='johndball/pisonde-mobile'
FILES={'mobile.py','field_features.py','field_ui.py','wifi_control.py','sdr_health.py','hardware.py','storage_maintenance.py','verify_recovery.py','update_manager.py','update_ui.py'}

def canonical(value):
    body={k:v for k,v in value.items() if k!='signature'}
    return DOMAIN+json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')

def key_id(public):
    return hashlib.sha256(public.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()

def exclusive(path,data,mode):
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
    with os.fdopen(descriptor,'wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())

def generate(folder,password):
    if not password:raise ValueError('A private-key password is required')
    folder=Path(folder).expanduser().resolve();folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    private_path=folder/'pisonde-release-private.pem';public_path=folder/'pisonde-release-public.pem'
    if private_path.exists() or public_path.exists():raise ValueError('Key files already exist; nothing overwritten')
    private=Ed25519PrivateKey.generate();public=private.public_key()
    exclusive(private_path,private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.BestAvailableEncryption(password.encode('utf-8'))),0o600)
    exclusive(public_path,public.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo),0o644)
    return private_path,public_path,key_id(public)

def load_private(path,password):
    raw=Path(path).read_bytes()
    if not raw.startswith(b'-----BEGIN ENCRYPTED PRIVATE KEY-----'):raise ValueError('Use an encrypted PKCS8 private key')
    key=serialization.load_pem_private_key(raw,password=password.encode('utf-8'))
    if not isinstance(key,Ed25519PrivateKey):raise ValueError('An Ed25519 private key is required')
    return key

def sign(value,private,sequence,days,clock=None):
    expected={'schema','compatibility','version','commit','files'}
    if set(value)!=expected or value['schema']!=2 or value['compatibility']!='app-only-v1':raise ValueError('Expected an unsigned schema-2 release candidate')
    if not re.fullmatch(r'\d+\.\d+\.\d+',value['version']) or not re.fullmatch('[0-9a-f]{40}',value['commit']):raise ValueError('Invalid candidate version/commit')
    if not isinstance(value['files'],dict) or set(value['files'])!=FILES or any(not re.fullmatch(r'[a-z_]+\.py',n) or not re.fullmatch('[0-9a-f]{64}',h) for n,h in value['files'].items()):raise ValueError('Invalid candidate file hashes')
    if type(sequence) is not int or not 1<=sequence<=2**53-1 or type(days) is not int or not 1<=days<=366:raise ValueError('Sequence must be positive; expiry must be 1–366 days')
    clock=clock or datetime.now(timezone.utc)
    value=dict(value,security=dict(repository=REPOSITORY,channel='signed-stable',sequence=sequence,
        issued_at=clock.strftime('%Y-%m-%dT%H:%M:%SZ'),expires_at=(clock+timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')))
    signature=private.sign(canonical(value));private.public_key().verify(signature,canonical(value))
    value['signature']=dict(algorithm='ed25519',key_id=key_id(private.public_key()),value=base64.b64encode(signature).decode('ascii'))
    return value

def verify(value,public):
    if not isinstance(public,Ed25519PublicKey):raise ValueError('An Ed25519 public key is required')
    meta=value['signature']
    if meta['algorithm']!='ed25519' or meta['key_id']!=key_id(public):raise ValueError('Signing key does not match')
    public.verify(base64.b64decode(meta['value'],validate=True),canonical(value))

def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    keygen=commands.add_parser('keygen');keygen.add_argument('--directory',type=Path,default=Path.home()/'.pisonde-signing')
    signer=commands.add_parser('sign');signer.add_argument('candidate',type=Path);signer.add_argument('--key',type=Path,default=Path.home()/'.pisonde-signing/pisonde-release-private.pem');signer.add_argument('--sequence',type=int,required=True);signer.add_argument('--days',type=int,default=90);signer.add_argument('--output',type=Path,required=True)
    checker=commands.add_parser('verify');checker.add_argument('manifest',type=Path);checker.add_argument('--public-key',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='keygen':
        password=getpass.getpass('Choose a signing-key password: ')
        if password!=getpass.getpass('Confirm signing-key password: '):raise ValueError('Passwords differ; nothing created')
        private,public,fingerprint=generate(args.directory,password)
        print('Private key (keep secret):',private);print('Public key (safe to share):',public);print('Public-key SHA256:',fingerprint)
        print('Back up the encrypted private key and its password separately. Never upload the private key.')
    elif args.command=='sign':
        candidate=json.loads(args.candidate.read_text(encoding='utf-8'))
        print('Release:',candidate.get('version'));print('Source commit:',candidate.get('commit'))
        print('Repository:',REPOSITORY);print('Manifest sequence:',args.sequence);print('Validity (days):',args.days)
        print('Compare the source commit and candidate checksums with your reviewed release before continuing.')
        if input('Type SIGN to authorize this release: ')!='SIGN':raise ValueError('Signing cancelled')
        private=load_private(args.key,getpass.getpass('Signing-key password: '))
        value=sign(candidate,private,args.sequence,args.days)
        exclusive(args.output,(json.dumps(value,indent=2)+'\n').encode('utf-8'),0o644)
        print('Signed manifest:',args.output);print('Public-key SHA256:',value['signature']['key_id']);print('Expires:',value['security']['expires_at'])
    else:
        public=serialization.load_pem_public_key(args.public_key.read_bytes());verify(json.loads(args.manifest.read_text(encoding='utf-8')),public);print('PASS: signature matches the supplied public key (device policy checks are separate).')

if __name__=='__main__':
    try:main()
    except Exception as exc:print('ERROR:',str(exc),file=sys.stderr);sys.exit(1)
