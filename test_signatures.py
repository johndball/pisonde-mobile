"""Real Ed25519 interoperability and fail-closed update-boundary tests."""
import base64,hashlib,importlib.util,json,os,shutil,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import update_manager as u
spec=importlib.util.spec_from_file_location('sign_release',Path(__file__).parent/'tools/sign_release.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)

class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name)
        for name,path in {'APP':root/'app','STATE':root/'state','BACKUPS':root/'backups','TRUST':root/'public.pem'}.items():
            mock=patch.object(u,name,path);mock.start();self.addCleanup(mock.stop)
        u.STATE.mkdir();u.APP.mkdir()
        self.private=Ed25519PrivateKey.generate()
        u.TRUST.write_bytes(self.private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo));u.TRUST.chmod(0o644)
        self.source={name:("VERSION='0.3.15'\n" if name=='mobile.py' else '# release file\n').encode() for name in u.FILES}
        self.candidate=dict(schema=2,compatibility='app-only-v1',version='0.3.15',commit='a'*40,files={n:hashlib.sha256(b).hexdigest() for n,b in self.source.items()})
        self.value=s.sign(self.candidate,self.private,1,90)
    def resign(self,value,key=None):
        value=json.loads(json.dumps(value));key=key or self.private
        value['signature']=dict(algorithm='ed25519',key_id=s.key_id(key.public_key()),value=base64.b64encode(key.sign(s.canonical(value))).decode())
        return value
    def test_python_signatures_verify_with_independent_openssl(self):
        self.assertEqual(u.authenticate(self.value,record=True),self.value)
        self.assertEqual(u.read(u.STATE/'signature-state.json')['key_id'],s.key_id(self.private.public_key()))
        self.assertEqual(u.canonical(self.value),s.canonical(self.value))
    def test_missing_trust_key_never_uses_downloaded_key(self):
        u.TRUST.unlink()
        with self.assertRaisesRegex(RuntimeError,'enrollment'):u.authenticate(self.value)
    def test_unsigned_legacy_manifest_is_rejected(self):
        for value in (dict(self.candidate,schema=1),self.candidate):
            with self.assertRaises(RuntimeError):u.authenticate(value)
    def test_tampered_payload_fields_rejected(self):
        for name,value in (('version','9.0.0'),('commit','b'*40),('files',dict(self.value['files'],**{'mobile.py':'c'*64}))):
            bad=dict(self.value,**{name:value})
            with self.assertRaisesRegex(RuntimeError,'verification failed'):u.authenticate(bad)
    def test_replacing_signature_or_key_id_fails(self):
        other=Ed25519PrivateKey.generate()
        for value in (self.resign(self.value,other),dict(self.value,signature=dict(self.value['signature'],value=base64.b64encode(b'X'*64).decode()))):
            with self.assertRaises(RuntimeError):u.authenticate(value)
    def test_wrong_repository_channel_and_unsupported_algorithm_fail(self):
        for security in (dict(self.value['security'],repository='other/project'),dict(self.value['security'],channel='other')):
            with self.assertRaisesRegex(RuntimeError,'repository or channel'):u.authenticate(self.resign(dict(self.value,security=security)))
        with self.assertRaises(RuntimeError):u.authenticate(dict(self.value,signature=dict(self.value['signature'],algorithm='none')))
    def test_expired_future_and_excessively_long_metadata_fail(self):
        now=datetime.now(timezone.utc)
        for value in (s.sign(self.candidate,self.private,1,1,now-timedelta(days=2)),s.sign(self.candidate,self.private,1,90,now+timedelta(days=1)),self.resign(dict(self.value,security=dict(self.value['security'],expires_at='2099-01-01T00:00:00Z')))):
            with self.assertRaises(RuntimeError):u.authenticate(value)
    def test_lower_sequence_and_same_sequence_different_payload_fail(self):
        newer=s.sign(self.candidate,self.private,2,90);u.authenticate(newer,record=True)
        with self.assertRaisesRegex(RuntimeError,'replay'):u.authenticate(self.value)
        conflict=self.resign(dict(newer,commit='b'*40))
        with self.assertRaisesRegex(RuntimeError,'sequence conflict'):u.authenticate(conflict)
        u.authenticate(newer)  # Identical metadata is reusable between checks.
    def test_sequence_must_be_integer_not_boolean(self):
        for number in (True,0,-1,2**53,'2'):
            value=self.resign(dict(self.value,security=dict(self.value['security'],sequence=number)))
            with self.assertRaises(RuntimeError):u.authenticate(value)
    def test_duplicate_json_fields_and_nan_are_rejected(self):
        for raw in ('{"schema":2,"schema":1}','{"schema":NaN}'):
            with self.assertRaises(RuntimeError):u.strict_json(raw)
    def test_key_permissions_and_symlink_rejected(self):
        u.TRUST.chmod(0o666)
        with self.assertRaisesRegex(RuntimeError,'protected'):u.authenticate(self.value)
        u.TRUST.chmod(0o644);real=u.TRUST.with_suffix('.real');u.TRUST.rename(real);u.TRUST.symlink_to(real)
        with self.assertRaises(RuntimeError):u.authenticate(self.value)
    def test_invalid_signature_rejected_before_download_or_service_stop(self):
        value=dict(self.value,commit='b'*40)
        with patch.object(u,'download') as download,patch.object(u,'services') as services:
            with self.assertRaises(RuntimeError):u.stage(value)
            with self.assertRaises(RuntimeError):u.apply(value,Path(self.temp.name))
        download.assert_not_called();services.assert_not_called()
    def test_valid_signature_does_not_bypass_file_checksum(self):
        with patch.object(u,'download',return_value=b'tampered file'):
            with self.assertRaisesRegex(RuntimeError,'Checksum'):u.stage(self.value)
    def test_staged_files_remain_signature_protected_offline(self):
        def download(url,limit):return self.source[url.rsplit('/',1)[1]]
        with patch.object(u,'download',side_effect=download):folder=u.stage(self.value)
        (folder/'mobile.py').write_text('# altered after verified download')
        with patch.object(u,'services') as services:
            with self.assertRaisesRegex(RuntimeError,'Staged content'):u.apply(self.value,folder)
        services.assert_not_called()
    def test_catalog_authenticates_and_records_exact_signed_content(self):
        entry=dict(encoding='base64',path='updates/signed-stable.json',content=base64.b64encode(json.dumps(self.value).encode()).decode())
        with patch.object(u,'download',return_value=json.dumps(entry).encode()):result=u.catalog()
        self.assertEqual(result,self.value);self.assertEqual(u.read(u.STATE/'signature-state.json')['sequence'],1)
    def test_invalid_channel_does_not_advance_trusted_sequence(self):
        tampered=dict(self.value,commit='b'*40)
        entry=dict(encoding='base64',path='updates/signed-stable.json',content=base64.b64encode(json.dumps(tampered).encode()).decode())
        with patch.object(u,'download',return_value=json.dumps(entry).encode()):
            with self.assertRaises(RuntimeError):u.catalog()
        self.assertFalse((u.STATE/'signature-state.json').exists())
    def test_keygen_creates_encrypted_private_and_refuses_overwrite(self):
        folder=Path(self.temp.name)/'publisher'
        private,public,fingerprint=s.generate(folder,'test-only-password')
        self.assertTrue(private.read_bytes().startswith(b'-----BEGIN ENCRYPTED PRIVATE KEY-----'))
        loaded=s.load_private(private,'test-only-password')
        self.assertEqual(fingerprint,s.key_id(loaded.public_key()));self.assertIn(b'BEGIN PUBLIC KEY',public.read_bytes())
        if os.name=='posix':self.assertEqual(private.stat().st_mode&0o777,0o600)
        with self.assertRaises(ValueError):s.generate(folder,'test-only-password')
        with self.assertRaises(ValueError):s.load_private(private,'wrong-password')
    def test_signer_does_not_modify_candidate_or_sign_invalid_scope(self):
        self.assertNotIn('security',self.candidate)
        with self.assertRaises(ValueError):s.sign(dict(self.candidate,schema=1),self.private,1,90)
        s.verify(self.value,self.private.public_key())
    def test_full_check_authenticates_network_and_stages_validated_files(self):
        (u.APP/'mobile.py').write_text("VERSION='0.3.14'\n")
        entry=dict(encoding='base64',path='updates/signed-stable.json',content=base64.b64encode(json.dumps(self.value).encode()).decode())
        def download(url,limit):
            return json.dumps(entry).encode() if url.startswith(u.API) else self.source[url.rsplit('/',1)[1]]
        with patch.object(u,'command'),patch.object(u,'download',side_effect=download),patch.object(u,'services') as services:u.run('check')
        services.assert_not_called();self.assertEqual(u.status()['outcome'],'available')
        self.assertTrue((u.STATE/'staged'/self.value['commit']/'mobile.py').exists())
        self.assertEqual(u.read(u.STATE/'signature-state.json')['key_id'],s.key_id(self.private.public_key()))
    def test_expired_cache_fetches_fresh_signed_metadata(self):
        (u.APP/'mobile.py').write_text("VERSION='0.3.14'\n")
        expired=s.sign(self.candidate,self.private,1,1,datetime.now(timezone.utc)-timedelta(days=2))
        u.put(u.STATE/'available.json',expired);u.update_status(last_check=u.now())
        renewed=s.sign(self.candidate,self.private,2,90)
        entry=dict(encoding='base64',path='updates/signed-stable.json',content=base64.b64encode(json.dumps(renewed).encode()).decode())
        def download(url,limit):return json.dumps(entry).encode() if url.startswith(u.API) else self.source[url.rsplit('/',1)[1]]
        with patch.object(u,'command'),patch.object(u,'download',side_effect=download),patch.object(u,'apply'):u.run('run')
        self.assertEqual(u.read(u.STATE/'signature-state.json')['sequence'],2)
    def test_signer_allowlist_matches_device_allowlist(self):
        self.assertEqual(s.FILES,u.FILES)
