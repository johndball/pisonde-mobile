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
 def test_health_row_is_plain_text_under_header_and_formats_units(self):
  import re,subprocess,shutil
  page=self.client.get('/').text
  self.assertIn('</header><div id="device-health" class="muted"',page)
  self.assertIn('healthText(s.ui_health||{})',page)
  if shutil.which('node'):
   function=re.search(r'function healthText\(h\)\{.*?\n\}',page,re.S).group()
   script=function+';console.log(healthText({cpu_percent:12,temp_c:50,disk_free_gib:100,disk_free_percent:88,power:"OK"}));console.log(healthText({}));'
   lines=subprocess.run(['node','-e',script],capture_output=True,text=True,check=True).stdout.splitlines()
   self.assertIn('Temp: 50°C / 122.0°F',lines[0]);self.assertIn('CPU: 12%',lines[0]);self.assertIn('Input voltage: N/A',lines[0]);self.assertIn('CPU: N/A',lines[1])
 def test_health_sampler_uses_cpu_deltas_temperature_and_power_flags(self):
  from types import SimpleNamespace
  proc=Path(self.tmp.name)/'stat';temp=Path(self.tmp.name)/'temp';temp.write_text('54500')
  proc.write_text('cpu 10 0 10 80 0 0 0 0\n');self.u.health_cpu=None
  with patch('subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='throttled=0x10001')):
   first=self.u.sample_health(proc,temp);self.assertIsNone(first['cpu_percent'])
   proc.write_text('cpu 20 0 20 160 0 0 0 0\n');second=self.u.sample_health(proc,temp)
  self.assertEqual(second['cpu_percent'],20);self.assertEqual(second['temp_c'],54.5);self.assertEqual(second['power'],'Undervoltage');self.assertGreater(second['disk_free_gib'],0)
  self.assertEqual(second['battery'],'N/A');self.assertEqual(second['input_voltage'],'N/A')
 def test_health_api_rejects_stale_cache(self):
  import time
  self.u.health_state=dict(checked_at=time.monotonic(),temp_c=50)
  self.assertEqual(self.status()['ui_health']['temp_c'],50)
  self.u.health_state['checked_at']-=30;self.assertEqual(self.status()['ui_health'],{})
 def test_adapter_ips_are_under_mac_lines(self):
  page=self.client.get('/wifi').text
  self.assertIn('Onboard AP IP:',page);self.assertIn('Linksys upstream IP:',page)
  self.assertIn('No IPv4 address',page);self.assertIn('white-space:pre-line',page)

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
 def test_weather_local_only_status_and_guidance(self):
  self.assertTrue(self.status()['ui_weather_local_only'])
  self.assertNotIn('ui_weather_approval',self.status())
  page=self.client.get('/sondehub-approval').text
  self.assertIn('Weather reception: local only',page)
  self.assertNotIn('Contact <a href="mailto:',page)
  self.assertIn('local only',self.client.get('/').text)
  self.assertIn('local display and history only',self.client.get('/uploads').text)
 def test_help_exports_and_controls_remain_available(self):
  for path in ['/','/help','/sondehub-approval','/wifi','/history','/uploads','/weather-validation.jsonl?type=RS41','/upload-report.json']:
   self.assertEqual(self.client.get(path).status_code,200,path)
  self.assertEqual(self.client.post('/api/control',json={'profile':'STOP'}).status_code,403)
  page=self.client.get('/').text;self.assertIn('v0.3.23',page);self.assertIn('id="upload-status"',page);self.assertNotIn('Auto: resume searching',page)
 def test_status_reads_do_not_touch_stored_history(self):
  self.add();before=self.m.DB.read_bytes();self.status();self.status()
  with self.m.database() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM receptions').fetchone()[0],1)


 def test_update_controls_and_csrf(self):
  self.assertEqual(self.client.get('/updates').status_code,200)
  self.assertIn('Automatic installation',self.client.get('/updates').text)
  self.assertIn('href="/updates"',self.client.get('/').text)
  self.assertEqual(self.client.post('/api/updates',json={'operation':'install'}).status_code,403)
  with self.client.session_transaction() as session:token=session['token']
  with patch.object(self.f,'broker',return_value={'automatic':False,'installed_version':'0.3.23'}) as broker:
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

 def test_altitude_units_keep_packet_reference_and_numeric_precision(self):
  import re,subprocess,shutil
  if not shutil.which('node'):self.skipTest('Node unavailable')
  page=self.client.get('/').text
  function=re.search(r'function altitudeText\(m\)\{.*?\}',page).group()
  result=subprocess.run(['node','-e',function+';console.log(JSON.stringify([altitudeText(26174.6),altitudeText(0),altitudeText(-10)]))'],capture_output=True,text=True,check=True)
  values=json.loads(result.stdout)
  self.assertEqual(values[0],'26174.6 m · 85874.7 ft · 16.26 mi')
  self.assertEqual(values[1],'0.0 m · 0.0 ft · 0.00 mi')
  self.assertTrue(values[2].startswith('-10.0 m · -32.8 ft'))
  self.assertIn('Altitude reported by packet:',page)
  self.assertIn('altitudeText(p.altitude_m)',page)

 def test_wifi_attempt_and_usb_reset_status_are_explicit(self):
  page=self.client.get('/wifi').text
  for text in ('Attempting:','client_note','Upstream USB resets:','showWifiStatus(value.ui_wifi)'):
   self.assertIn(text,page)
  self.assertIn('w.client_note',self.client.get('/').text)
  import re,subprocess,shutil
  if shutil.which('node'):
   helpers=re.search(r'(function showWifiHealth\(s\).*?)setInterval',page,re.S).group(1)
   states=[dict(ap_mac='02:00:00:00:00:01',client_mac='02:00:00:00:00:02',ap_present=True,hotspot='Field hotspot',recovery={}),dict(ap_mac='02:00:00:00:00:01',client_mac='02:00:00:00:00:02',ap_present=True,hotspot='',client_note='Connecting to upstream Wi-Fi',attempted_hotspot='Field hotspot',recovery={}),dict(note='status unavailable')]
   script='const nodes={};const $=id=>nodes[id]||(nodes[id]={});'+helpers+';for(const s of '+json.dumps(states)+'){showWifiStatus(s);console.log(nodes.state.textContent);}'
   result=subprocess.run(['node','-e',script],text=True,capture_output=True,check=True)
   lines=result.stdout.splitlines()
   self.assertIn('Upload connection: Field hotspot',lines[0])
   self.assertIn('Upload connection: Connecting to upstream Wi-Fi',lines[1])
   self.assertIn('Attempting: Field hotspot',lines[1])
   self.assertEqual(lines[2],'status unavailable')


if __name__=='__main__':unittest.main()

