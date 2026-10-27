import copy,importlib.util,json,logging,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
HERE=Path(__file__).parent
class GuiTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  def load(name):
   spec=importlib.util.spec_from_file_location(name,HERE/(name+'.py'));m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m
  self.m=load('mobile');m=self.m;m.ROOT=Path(self.tmp.name);m.DB=m.ROOT/'db.sqlite3';m.CONFIGFILE=m.ROOT/'scanner.json';m.RUNTIME=m.ROOT/'runtime';m.LOGROOT=m.ROOT/'logs';m.config=dict(m.DEFAULT);m.setup();m.app.secret_key='test'
  self.f=load('field_features');self.f.install(m,start_threads=False);self.u=load('field_ui');self.u.install(m,start_threads=False);self.client=m.app.test_client()
 def tearDown(self):self.m.shutdown_event.set();logging.shutdown()
 def add(self,call='X1234567',protocol='RS41',lat=38.,lon=-77.,when='2026-10-23T13:16:38Z'):
  with self.m.database() as db:
   cursor=db.execute('INSERT INTO receptions(received_utc,protocol,frequency_mhz,callsign,latitude,longitude,altitude_m,raw_packet) VALUES(?,?,?,?,?,?,?,?)',(when,protocol,403. if protocol=='RS41' else 144.39,call,lat,lon,9500.,'{"frame":"test"}'))
   return cursor.lastrowid
 def status(self):return self.client.get('/api/status').json
 def test_latest_position_is_kept_when_newest_packet_has_no_coordinates(self):
  pos=self.add();last=self.add(lat=None,lon=None);s=self.status();self.assertEqual(len(s['ui_targets']),1);self.assertEqual(s['ui_targets'][0]['id'],pos);self.assertEqual(s['ui_targets'][0]['last_id'],last)
 def test_earlier_history_and_incremental_updates(self):
  old=self.add();new=self.add();s=self.status();self.assertEqual(s['ui_targets'][0]['id'],new);self.assertEqual(s['ui_targets'][0]['earlier'][0]['id'],old)
  later=self.add();s=self.status();self.assertEqual(s['ui_targets'][0]['id'],later);self.assertEqual(s['ui_targets'][0]['earlier'][0]['id'],new)
 def test_history_groups_survive_more_than_thirty_new_packets(self):
  first=self.add(call='RECOVERY');self.status()
  for _ in range(35):self.add(call='ACTIVE')
  s=self.status();self.assertEqual({x['callsign'] for x in s['ui_targets']},{'RECOVERY','ACTIVE'})
 def test_same_callsign_different_protocols_remain_separate(self):
  self.add(call='TEST',protocol='RS41');self.add(call='TEST',protocol='APRS');self.assertEqual(len(self.status()['ui_targets']),2)
 def test_specific_approval_error_is_named_and_survives_expiration_in_session(self):
  data=self.f.upload_status();data['destinations']['sondehub']['last']={'status':'pending','last_error':'This software is unknown to us!'}
  with patch.object(self.f,'upload_status',return_value=data):self.assertTrue(self.status()['ui_weather_approval'])
  data['destinations']['sondehub']['last']={'status':'expired','last_error':'Too old'}
  with patch.object(self.f,'upload_status',return_value=data):self.assertTrue(self.status()['ui_weather_approval'])
  data['destinations']['sondehub']['last']={'status':'accepted','last_error':''}
  with patch.object(self.f,'upload_status',return_value=data):self.assertFalse(self.status()['ui_weather_approval'])
 def test_generic_upload_failure_is_not_called_approval(self):
  data=self.f.upload_status();data['destinations']['sondehub']['last']={'status':'pending','last_error':'Network unreachable'}
  with patch.object(self.f,'upload_status',return_value=data):self.assertFalse(self.status()['ui_weather_approval'])
 def test_help_exports_and_controls_remain_available(self):
  for path in ['/','/help','/sondehub-approval','/wifi','/history','/uploads','/weather-validation.jsonl?type=RS41','/upload-report.json']:
   self.assertEqual(self.client.get(path).status_code,200,path)
  self.assertEqual(self.client.post('/api/control',json={'profile':'STOP'}).status_code,403)
  page=self.client.get('/').text;self.assertIn('v0.3.18',page);self.assertIn('id="upload-status"',page);self.assertNotIn('Auto: resume searching',page)
 def test_status_reads_do_not_touch_stored_history(self):
  self.add();before=self.m.DB.read_bytes();self.status();self.status()
  with self.m.database() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM receptions').fetchone()[0],1)


 def test_update_controls_and_csrf(self):
  self.assertEqual(self.client.get('/updates').status_code,200)
  self.assertIn('Automatic installation',self.client.get('/updates').text)
  self.assertIn('href="/updates"',self.client.get('/').text)
  self.assertEqual(self.client.post('/api/updates',json={'operation':'install'}).status_code,403)
  with self.client.session_transaction() as session:token=session['token']
  with patch.object(self.f,'broker',return_value={'automatic':False,'installed_version':'0.3.18'}) as broker:
   response=self.client.post('/api/updates',json={'operation':'set_auto','enabled':False},headers={'X-CSRF-Token':token})
   self.assertEqual(response.status_code,200);broker.assert_called_once_with('update',operation='set_auto',enabled=False)

 def test_update_policy_api_and_controls(self):
  page=self.client.get('/updates').text
  for ident in ('scheduled','reboot_if_missed','immediate','window','save-policy'):
   self.assertIn('id="'+ident+'"',page)
  self.assertNotIn('3 and 5 a.m.',page)
  with self.client.session_transaction() as session:token=session['token']
  cfg=dict(scheduled=True,reboot_if_missed=True,immediate=False,window_start=5,window_end=7)
  with patch.object(self.f,'broker',return_value=cfg) as broker:
   response=self.client.post('/api/updates',json={'operation':'set_policy','settings':cfg},headers={'X-CSRF-Token':token})
   self.assertEqual(response.status_code,200);broker.assert_called_once_with('update',operation='set_policy',settings=cfg)
  self.assertEqual(self.client.post('/api/updates',json={'operation':'set_policy','settings':cfg}).status_code,403)

 def test_updates_javascript_parses(self):
  import re,subprocess,shutil
  if not shutil.which('node'):self.skipTest('Node is unavailable')
  for script in re.findall(r'<script>(.*?)</script>',self.client.get('/updates').text,re.S):
   result=subprocess.run(['node','--check'],input=script,text=True,capture_output=True)
   self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()

