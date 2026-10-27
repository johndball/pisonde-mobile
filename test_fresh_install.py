"""Fresh-install enrollment integration using real signatures and temporary files."""
import base64,hashlib,importlib.util,json,os,shutil,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import fresh_install as f
import update_manager as u
spec=importlib.util.spec_from_file_location('fresh_test_signer',Path(__file__).parent/'tools/sign_release.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)

class FreshInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir();(self.source/'updates').mkdir();self.cache=self.root/'verified.json'
        for name,path in {'APP':self.root/'app','STATE':self.root/'state','TRUST':self.root/'etc'/'update-signing-public.pem','HARDWARE':self.root/'hardware.json','DATA':self.root/'history.sqlite3'}.items():
            mock=patch.object(u,name,path);mock.start();self.addCleanup(mock.stop)
        u.APP.mkdir()
        self.key=Ed25519PrivateKey.generate();self.public=self.key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        (self.source/'updates/signing-public.pem').write_bytes(self.public)
        fingerprint=patch.object(f.verifier,'FINGERPRINT',s.key_id(self.key.public_key()));fingerprint.start();self.addCleanup(fingerprint.stop)
        self.files={name:("VERSION='0.3.16'\n" if name=='mobile.py' else '# verified release\n').encode() for name in u.FILES}
        for name,data in self.files.items():(self.source/name).write_bytes(data);(u.APP/name).write_bytes(data)
        self.candidate=dict(schema=2,compatibility='app-only-v1',version='0.3.16',commit='a'*40,files={n:hashlib.sha256(b).hexdigest() for n,b in self.files.items()})
        self.value=s.sign(self.candidate,self.key,2,90);self.cache.write_text(json.dumps(self.value))
    def entry(self,value=None,path='updates/releases/0.3.16.json'):
        return json.dumps(dict(encoding='base64',path=path,content=base64.b64encode(json.dumps(value or self.value).encode()).decode())).encode()
    def test_preflight_verifies_versioned_signed_source(self):
        with patch.object(f.verifier,'fetch',return_value=self.entry()) as fetch:value=f.preflight(self.source,self.cache)
        self.assertEqual(value,self.value);self.assertIn('updates/releases/0.3.16.json',fetch.call_args.args[0]);self.assertEqual(self.cache.stat().st_mode&0o777,0o600)
    def test_full_install_enrolls_key_without_hardware_or_network(self):
        with patch.object(f.verifier,'fetch',return_value=self.entry()):f.preflight(self.source,self.cache)
        with patch.object(f.verifier,'fetch') as fetch,patch.object(u,'services') as services:f.enroll_fresh(self.source,self.cache)
        fetch.assert_not_called();services.assert_not_called();self.assertEqual(u.trusted_key()[1],s.key_id(self.key.public_key()))
        self.assertEqual(u.read(u.STATE/'signature-state.json')['sequence'],2)
        self.assertTrue(u.status()['signing_enrolled']);self.assertEqual(u.status()['installed_version'],'0.3.16')
    def test_service_owned_directory_is_fixed_without_changing_files(self):
        u.TRUST.parent.mkdir();u.TRUST.parent.chmod(0o775)
        other=u.TRUST.parent/'existing.conf';other.write_text('preserved');original_owner=(other.stat().st_uid,other.stat().st_gid)
        # The test host cannot map the Pi service UID. Model its lstat owner,
        # while executing the real root chown/chmod and signature checks.
        owner=[999,999];real_lstat=Path.lstat;real_chown=os.chown
        def lstat(path,*args,**kwargs):
            info=real_lstat(path,*args,**kwargs)
            if path==u.TRUST.parent:
                values=list(info);values[4:6]=owner;return os.stat_result(values)
            return info
        def chown(path,uid,gid):
            self.assertEqual(Path(path),u.TRUST.parent);owner[:]=[uid,gid];real_chown(path,uid,gid)
        with patch.object(Path,'lstat',lstat),patch.object(f.os,'chown',side_effect=chown) as changes:
            f.enroll_fresh(self.source,self.cache)
        changes.assert_called_once_with(u.TRUST.parent,0,0)
        info=u.TRUST.parent.stat();self.assertEqual((info.st_uid,info.st_gid,info.st_mode&0o777),(0,0,0o755))
        self.assertEqual((other.stat().st_uid,other.stat().st_gid),original_owner);self.assertEqual(other.read_text(),'preserved')
        self.assertEqual(u.TRUST.stat().st_uid,0);self.assertEqual(u.TRUST.stat().st_mode&0o777,0o644)
    def test_configuration_directory_symlink_is_rejected(self):
        outside=self.root/'outside';outside.mkdir();u.TRUST.parent.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError,'real directory'):f.enroll_fresh(self.source,self.cache)
        self.assertFalse((outside/u.TRUST.name).exists());self.assertFalse(u.STATE.exists())
    def test_tampered_source_refused_before_enrollment(self):
        (self.source/'mobile.py').write_text("VERSION='0.3.16'\n# tampered\n")
        with self.assertRaisesRegex(RuntimeError,'differs'):f.enroll_fresh(self.source,self.cache)
        self.assertFalse(u.TRUST.exists());self.assertFalse(u.STATE.exists())
    def test_tampered_installed_copy_refused_before_enrollment(self):
        (u.APP/'wifi_control.py').write_text('# changed after copy')
        with self.assertRaisesRegex(RuntimeError,'differs'):f.enroll_fresh(self.source,self.cache)
        self.assertFalse(u.TRUST.exists())
    def test_expired_metadata_refused(self):
        old=s.sign(self.candidate,self.key,2,1,datetime.now(timezone.utc)-timedelta(days=2));self.cache.unlink()
        with patch.object(f.verifier,'fetch',return_value=self.entry(old)):
            with self.assertRaises(RuntimeError):f.preflight(self.source,self.cache)
        self.assertFalse(self.cache.exists());self.assertFalse(u.TRUST.exists())
    def test_wrong_key_refused(self):
        other=Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        (self.source/'updates/signing-public.pem').write_bytes(other)
        with self.assertRaisesRegex(RuntimeError,'fingerprint'):f.enroll_fresh(self.source,self.cache)
        self.assertFalse(u.TRUST.exists())
    def test_modified_cached_manifest_refused(self):
        self.cache.write_text(json.dumps(dict(self.value,commit='b'*40)))
        with self.assertRaisesRegex(RuntimeError,'signature'):f.enroll_fresh(self.source,self.cache)
        self.assertFalse(u.TRUST.exists())
    def test_configured_device_or_history_refused(self):
        for path in (u.HARDWARE,u.DATA):
            path.write_text('existing data')
            with self.assertRaisesRegex(RuntimeError,'Already configured'):f.enroll_fresh(self.source,self.cache)
            self.assertEqual(path.read_text(),'existing data');path.unlink()
        self.assertFalse(u.TRUST.exists())
    def test_existing_different_trust_key_not_replaced(self):
        u.TRUST.parent.mkdir();other=Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo);u.TRUST.write_bytes(other)
        with self.assertRaisesRegex(RuntimeError,'differs'):f.enroll_fresh(self.source,self.cache)
        self.assertEqual(u.TRUST.read_bytes(),other)
    def test_manifest_for_wrong_version_path_refused(self):
        with patch.object(f.verifier,'fetch',return_value=self.entry(path='updates/releases/0.3.15.json')):
            with self.assertRaises(RuntimeError):f.preflight(self.source,self.cache)
    def test_installer_verifies_before_copy_and_enrolls_after_build(self):
        script=Path(__file__).with_name('install.sh').read_text()
        self.assertLess(script.index('fresh_install.py" preflight'),script.index('install -m 0644 "$SOURCE"/*.py'))
        self.assertGreater(script.index('fresh_install.py" enroll'),script.index('make -C'))
        self.assertIn('install -d -o root -g root -m 0755 "$DEST" "$DEST/bin" /etc/pisonde-mobile',script)
        self.assertIn('[ ! -L /etc/pisonde-mobile ]',script)
