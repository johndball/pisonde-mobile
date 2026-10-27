"""Release publisher policy checks; GitHub writes are mocked."""
import importlib.util,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('release_publisher',Path(__file__).parent/'tools/publish_releases.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'release-notes').mkdir();(self.root/'release-notes/v0.3.13.md').write_text('Release notes')
        mock=patch.object(p,'ROOT',self.root);mock.start();self.addCleanup(mock.stop)
        self.stable=dict(version='0.3.13',commit='a'*40);self.entry=dict(tag='v0.3.13',commit='stable')
    def test_tags_and_release_use_exact_stable_commit(self):
        calls=[]
        def request(method,path,body=None):
            calls.append((method,path,body));return {'sha':'a'*40} if path.startswith('/git/commits') else None
        with patch.object(p,'request',side_effect=request):p.publish(self.entry,self.stable)
        tag=next(x[2] for x in calls if x[:2]==('POST','/git/refs'));self.assertEqual(tag['sha'],'a'*40)
        release=next(x[2] for x in calls if x[:2]==('POST','/releases'));self.assertEqual(release['body'],'Release notes');self.assertEqual(release['make_latest'],'true')
    def test_existing_matching_release_is_not_recreated(self):
        def request(method,path,body=None):
            if path.startswith('/git/commits'):return {'sha':'a'*40}
            if path.startswith('/git/ref'):return {'object':{'type':'commit','sha':'a'*40}}
            if path.startswith('/releases'):return dict(id=1,name='PiSonde Mobile 0.3.13',body='Release notes',prerelease=False)
        with patch.object(p,'request',side_effect=request) as request:p.publish(self.entry,self.stable)
        self.assertTrue(all(c.args[0]=='GET' for c in request.call_args_list))
    def test_existing_tag_is_never_moved(self):
        def request(method,path,body=None):
            return {'object':{'type':'commit','sha':'b'*40}} if path.startswith('/git/ref') else {'sha':'a'*40}
        with patch.object(p,'request',side_effect=request):
            with self.assertRaises(RuntimeError):p.publish(self.entry,self.stable)
    def test_future_version_waits_for_channel_publication(self):
        with patch.object(p,'request') as request:p.publish(self.entry,dict(version='0.3.12',commit='b'*40))
        request.assert_not_called()

    def test_signed_release_waits_until_signature_is_published(self):
        with patch.object(p,'request') as request:p.publish(dict(tag='v0.3.15',commit='signed-stable'),dict(version='0.3.14',commit='b'*40))
        request.assert_not_called()
    def test_signed_release_uses_signed_source_commit(self):
        calls=[]
        def request(method,path,body=None):calls.append((method,path,body));return {'sha':'a'*40} if path.startswith('/git/commits') else None
        (self.root/'release-notes/v0.3.15.md').write_text('Signed release')
        with patch.object(p,'request',side_effect=request):p.publish(dict(tag='v0.3.15',commit='signed-stable'),dict(schema=2,version='0.3.15',commit='a'*40))
        self.assertEqual(next(c[2]['sha'] for c in calls if c[:2]==('POST','/git/refs')),'a'*40)
