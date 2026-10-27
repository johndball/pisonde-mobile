import binascii
from datetime import datetime,timedelta,timezone
import gzip
import importlib.util
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

HERE=Path(__file__).parent

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,HERE/file)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module

class FieldTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.m=load('field_test_mobile','mobile.py')
        m=self.m;m.ROOT=root;m.DB=root/'receptions.sqlite3';m.CONFIGFILE=root/'scanner.json';m.LOGROOT=root/'logs';m.RUNTIME=root/'runtime'
        m.setup();m.app.secret_key='test-only'
        self.f=load('field_test_features','field_features.py');self.f.install(m,start_threads=False)
        self.f.SETTINGS.update(enabled=True,callsign='N0TEST-3');(root/'uploads.json').write_text(json.dumps(self.f.SETTINGS))
        self.client=m.app.test_client();self.client.get('/')
        with self.client.session_transaction() as session:self.headers={'X-CSRF-Token':session['token']}
    def tearDown(self):self.m.shutdown_event.set();self.tmp.cleanup()
    def post(self,url,value=None):return self.client.post(url,json=value,headers=self.headers)
    def packet(self,mode='APRS',raw='[0.2] N0TEST>APZPSM:>Receiver test'):
        return self.m.store_packet(self.m.parse_packet(raw,mode),mode,144390000 if mode=='APRS' else 404000000)
    def horus(self,ident=0):
        now=datetime.now(timezone.utc)
        raw=struct.pack('<BH3sffHBBbB',ident,42,bytes([now.hour,now.minute,now.second]),38.,-78.,1000,10,8,20,150)
        raw+=struct.pack('<H',binascii.crc_hqx(raw,0xffff))
        return raw.hex()
    def jobs(self):
        with self.m.database() as db:return [dict(r) for r in db.execute('SELECT * FROM upload_jobs')]
    def test_aprs_queue_filter_and_no_old_replay(self):
        self.packet();self.assertEqual(len(self.jobs()),1)
        self.assertIn(',qAO,N0TEST-3:',self.jobs()[0]['payload'])
        for path in ('NOGATE','RFONLY','TCPIP*','qAR,OTHER'):
            self.packet(raw='[0.2] N0TEST>APZPSM,'+path+':>Do not forward')
        self.assertEqual(len(self.jobs()),1)
        self.assertIsNone(self.f.aprs_gate('N0TEST>APZPSM:}OTHER>APRS:!position','N0TEST-3'))
        self.assertIsNone(self.f.aprs_gate('N0TEST>APZPSM:?APRS?','N0TEST-3'))
        with patch.object(self.f,'send_aprs') as send:
            self.f.upload_cycle();self.assertTrue(send.called)
        self.assertEqual(self.jobs()[0]['status'],'sent')
        with patch.object(self.f,'send_aprs') as send:
            self.f.upload_cycle();self.assertFalse(send.called)
    def test_horus_reserved_id_always_dev_and_local_only(self):
        r=self.post('/api/test-horus',{'hex':self.horus()});self.assertEqual(r.status_code,200)
        payload=json.loads(self.jobs()[0]['payload']);self.assertIn('dev',payload)
        self.assertEqual(payload['payload_callsign'],'4FSKTEST');self.assertNotIn('uploader_position',payload)
        self.assertEqual(self.post('/api/test-horus',{'hex':'00'*22}).status_code,400)
        self.assertEqual(self.client.post('/api/test-horus',json={'hex':self.horus()},headers=self.headers,environ_overrides={'REMOTE_ADDR':'192.168.73.30'}).status_code,403)
    def test_real_horus_uses_live_path(self):
        import horusdemodlib.payloads as payloads
        with patch.dict(payloads.HORUS_PAYLOAD_LIST,{1:'N0TEST-11'}):
            self.packet('HORUS',self.horus(1))
        self.assertEqual(self.jobs()[0]['destination'],'amateur')
        self.assertNotIn('dev',json.loads(self.jobs()[0]['payload']))
    def test_weather_stale_and_bad_position_withheld(self):
        for raw in (dict(id='X1234567',datetime=self.f.stamp(self.f.utc()-timedelta(hours=1)),lat=38.,lon=-78.,alt=1000,frame=1),
                    dict(id='DFM-123456789',datetime=self.f.stamp(self.f.utc()),lat=0.,lon=0.,alt=1000,sats=2)):
            self.packet('RS41' if raw['id'].startswith('X') else 'DFM',json.dumps(raw))
        self.assertEqual(self.jobs(),[])
    def test_retry_expiry_and_durable_queue(self):
        self.packet();self.f.load_settings();self.f.create_schema();self.assertEqual(len(self.jobs()),1)
        with patch.object(self.f,'send_aprs',side_effect=OSError('offline')):self.f.upload_cycle()
        job=self.jobs()[0];self.assertEqual(job['attempts'],1);self.assertEqual(job['status'],'pending')
        with self.m.database() as db:db.execute('UPDATE upload_jobs SET created_utc=?,next_attempt=0',(self.f.stamp(self.f.utc()-timedelta(minutes=5)),))
        with patch.object(self.f,'send_aprs') as send:self.f.upload_cycle();self.assertFalse(send.called)
        self.assertEqual(self.jobs()[0]['status'],'expired')
        with self.m.database() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM receptions').fetchone()[0],1)
    def test_sondehub_gzip_validation_and_retry(self):
        self.post('/api/test-horus',{'hex':self.horus()})
        class Reply:
            status_code=200;text='ok'
            def json(self):return {'message':'ok','errors':[]}
        class FakeHTTP:
            def __init__(self):self.requests=[]
            def put(self,url,**kwargs):self.requests.append((url,kwargs));return Reply()
        http=FakeHTTP();self.f.upload_cycle(http)
        self.assertEqual(self.jobs()[0]['status'],'test_passed')
        url,request=http.requests[0];self.assertTrue(url.endswith('/amateur/telemetry'))
        out=json.loads(gzip.decompress(request['data']))[0];self.assertIn('dev',out)
        self.assertNotIn('/listeners',url);self.assertNotIn('uploader_position',out)
    def test_development_validation_notice_is_success(self):
        self.post('/api/test-horus',{'hex':self.horus()})
        class Development:
            status_code=202;text='development validation passed'
            def json(self):return {'errors':[{'error_message':'All checks passed however payload contained dev flag so will not be uploaded to the database'}],'warnings':[]}
        with patch.object(self.f.requests,'put',return_value=Development()):self.f.upload_cycle()
        self.assertEqual(self.jobs()[0]['status'],'test_passed')
        self.assertEqual(self.jobs()[0]['attempts'],0)
    def test_upload_worker_uses_thirty_second_batches(self):
        with patch.object(self.m.shutdown_event,'wait',return_value=True) as wait:
            self.f.uploader()
        wait.assert_called_once_with(30)
    def test_sondehub_embedded_error_not_marked_accepted(self):
        self.post('/api/test-horus',{'hex':self.horus()})
        class Bad:
            status_code=200;text='error'
            def json(self):return {'errors':['bad data']}
        with patch.object(self.f.requests,'put',return_value=Bad()):self.f.upload_cycle()
        self.assertEqual(self.jobs()[0]['status'],'pending');self.assertEqual(self.jobs()[0]['attempts'],1)
    def weather(self, mode='DFM',count=3,ident=None,**fields):
        now=self.f.utc()
        for seq in range(count):
            raw=dict(id=ident or ('DFM-123456789' if mode=='DFM' else 'X1234567'),
                     datetime=self.f.stamp(now+timedelta(seconds=seq)),lat=38.,lon=-78.,alt=1000.,sats=8,frame=100+seq)
            raw.update(fields)
            self.packet(mode,json.dumps(raw))

    def test_sondehub_unrecognized_response_never_counts_accepted(self):
        class Reply:
            status_code=200;text='invalid response'
            def json(self):return []
        for value in ([],None,{}, {'message':'partial','errors':[]}):
            reply=Reply()
            reply.json=lambda:value
            if isinstance(value,dict) and value.get('message'):reply.status_code=202
            with self.assertRaises(RuntimeError):self.f.validate_sondehub_response(reply,[{'type':'DFM'}])
        reply=Reply();reply.json=lambda:(_ for _ in ()).throw(ValueError('html'))
        with self.assertRaises(RuntimeError):self.f.validate_sondehub_response(reply,[{'type':'DFM'}])

    def legacy_weather_job(self,status='pending'):
        self.weather('RS41',count=1)
        with self.m.database() as db:
            ident=db.execute('SELECT MAX(id) FROM receptions').fetchone()[0]
            db.execute('INSERT INTO upload_jobs(reception_id,destination,payload,status,created_utc) VALUES(?,?,?,?,?)',
                       (ident,'sondehub','{"serial":"X1234567","frame":100}',status,self.f.stamp(self.f.utc())))
        return ident

    def test_weather_positions_remain_local_without_upload_jobs(self):
        self.weather('DFM');self.weather('RS41')
        self.assertEqual(self.jobs(),[])
        with self.m.database() as db:
            rows=db.execute('SELECT * FROM receptions').fetchall()
        self.assertEqual(len(rows),6)
        self.assertTrue(all(r['latitude']==38. and r['longitude']==-78. for r in rows))
        self.assertIn('X1234567',self.client.get('/history.csv').text)
        self.assertEqual(self.client.get('/track.gpx?protocol=DFM').text.count('<trkpt '),3)
        with self.assertRaisesRegex(ValueError,'local-only'):
            self.f.format_sonde(rows[0],self.f.SETTINGS)

    def test_upgrade_disables_legacy_weather_queue_and_preserves_history(self):
        self.legacy_weather_job()
        self.f.create_schema();self.f.create_schema()
        job=self.jobs()[0]
        self.assertEqual(job['status'],'disabled');self.assertEqual(job['attempts'],0)
        self.assertIn('local history retained',job['last_error'])
        self.assertEqual(json.loads(job['payload'])['serial'],'X1234567')
        with self.m.database() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM receptions').fetchone()[0],1)

    def test_legacy_weather_is_never_sent_even_when_uploads_paused_or_reenabled(self):
        self.legacy_weather_job()
        self.f.SETTINGS['enabled']=False
        with patch.object(self.f.requests,'put') as send:
            self.f.upload_cycle();send.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'],'disabled')
        self.f.SETTINGS['enabled']=True
        self.packet()
        with patch.object(self.f.requests,'put') as send,patch.object(self.f,'send_aprs') as aprs:
            self.f.upload_cycle();send.assert_not_called();aprs.assert_called_once()
        self.assertEqual([j['status'] for j in self.jobs()],['disabled','sent'])

    def test_archival_weather_export_preserves_older_payload_without_uploading(self):
        self.legacy_weather_job();self.f.create_schema()
        with patch.object(self.f.requests,'put') as send:
            result=self.client.get('/weather-validation.jsonl?type=RS41');send.assert_not_called()
        self.assertEqual(result.text,self.jobs()[0]['payload']+'\n')
        self.assertEqual(self.client.get('/weather-validation.jsonl?type=DFM').text,'')
        self.assertEqual(self.client.get('/weather-validation.jsonl?type=APRS').status_code,400)
        report=self.client.get('/upload-report.json').json
        self.assertEqual(report['destinations']['sondehub']['counts'],{'disabled':1})

    def test_weather_historical_completed_jobs_are_unchanged(self):
        self.legacy_weather_job('expired');self.f.create_schema()
        self.assertEqual(self.jobs()[0]['status'],'expired')

    def test_endpoint_diagnostics_contact_only_amateur(self):
        class Reply:
            status_code=200;text='ok'
            def json(self):return {'message':'ok'}
        with patch.object(self.f.requests,'put',return_value=Reply()) as send:
            response=self.post('/api/upload-test')
        self.assertEqual(response.status_code,200);send.assert_called_once()
        self.assertEqual(set(response.json),{'amateur'})
        self.assertEqual(set(self.f.ENDPOINTS),{'amateur'})
        self.assertTrue(send.call_args.args[0].endswith('/amateur/telemetry'))
        payload=json.loads(gzip.decompress(send.call_args.kwargs['data']))[0]
        self.assertIn('dev',payload)
        self.assertNotIn('type',payload)

    def test_history_notes_csv_gpx_and_position_time(self):
        self.packet(raw='[0.2] N0TEST>APZPSM:!3800.00N/07800.00WO/A=003281')
        self.packet()
        r=self.client.get('/api/targets?query=N0T');point=r.json[0]
        self.assertEqual(point['packets'],2);self.assertEqual(point['latitude'],38.);self.assertIsNotNone(point['position_utc'])
        r=self.post('/api/notes',dict(protocol='APRS',callsign='N0TEST',note='Wait for dry terrain',recovery_status='Recovery planned'))
        self.assertEqual(r.status_code,200);self.assertEqual(self.client.get('/api/targets').json[0]['note'],'Wait for dry terrain')
        csv=self.client.get('/history.csv?target=N0TEST&protocol=APRS').text;self.assertIn('003281',csv)
        xml=ET.fromstring(self.client.get('/track.gpx?target=N0TEST&protocol=APRS').data)
        points=xml.findall('.//{http://www.topografix.com/GPX/1/1}trkpt');self.assertEqual(len(points),1)
        self.assertEqual(points[0].attrib['lat'],'38.0')
        self.assertEqual(self.client.get('/api/targets?query=%27%20OR%201=1').json,[])
    def test_controls_csrf_settings_pause_and_wifi(self):
        self.assertEqual(self.client.post('/api/wifi',json={'action':'scan'}).status_code,403)
        self.assertEqual(self.post('/api/wifi',{'action':'execute','cmd':'bad'}).status_code,400)
        with patch.object(self.f,'broker',return_value=[dict(ssid='Phone',signal='99')]) as broker:
            self.assertEqual(self.post('/api/wifi',dict(action='scan')).json[0]['ssid'],'Phone');broker.assert_called_once_with(action='scan')
        self.assertEqual(self.post('/api/uploads',dict(enabled=False,callsign='n0test-3')).status_code,200)
        self.packet();self.assertEqual(self.jobs(),[]);self.assertEqual(self.post('/api/upload-test').status_code,400)
        self.assertEqual(self.post('/api/uploads',dict(callsign='<bad>')).status_code,400)
        self.f.load_settings();self.assertFalse(self.f.SETTINGS['enabled'])
    def test_all_pages_render_valid_js_and_field_status(self):
        for url in ('/','/wifi','/history','/uploads'):
            result=self.client.get(url);self.assertEqual(result.status_code,200)
            for script in re.findall(r'<script>(.*?)</script>',result.text,re.S):
                path=Path(self.tmp.name)/'script.js';path.write_text(script)
                check=subprocess.run(['node','--check',str(path)],capture_output=True,text=True) if __import__('shutil').which('node') else None
                if check:self.assertEqual(check.returncode,0,check.stderr)
        status=self.client.get('/api/status').json;self.assertIn('upload',status);self.assertIn('heartbeat_age',status)
        self.assertIn('Recovery history',self.client.get('/').text);self.assertNotIn('not configured yet',self.client.get('/').text)
    def test_stop_mode_sends_watchdog(self):
        self.m.config['profile']='STOP';calls=[]
        def pulse():
            calls.append(1)
            if len(calls)>=2:self.m.shutdown_event.set()
        with patch.object(self.m,'heartbeat',side_effect=pulse),patch.object(self.m.shutdown_event,'wait',return_value=False):self.m.scanner()
        self.assertGreaterEqual(len(calls),2)

class WifiTests(unittest.TestCase):
    def setUp(self):self.w=load('field_test_wifi','wifi_control.py')
    def test_single_value_mac_is_literal_and_scan_escaping_preserved(self):
        from types import SimpleNamespace
        def command(args,**kwargs):
            if '-g' in args:
                value=self.w.AP_MAC if args[args.index('--escape')+1]=='no' else self.w.AP_MAC.replace(':',r'\:')
            else:value=r'John\:phone:90:WPA2'
            return SimpleNamespace(returncode=0,stdout=value,stderr='')
        with patch.object(self.w.subprocess,'run',side_effect=command) as run:
            self.assertEqual(self.w.nm('-g','802-11-wireless.mac-address','connection','show',self.w.AP),self.w.AP_MAC)
            output=self.w.nm('-t','-f','SSID,SIGNAL,SECURITY','device','wifi','list')
            self.assertEqual(self.w.fields(output),['John:phone','90','WPA2'])
            self.assertEqual(run.call_args.args[0][4],'yes')
    def test_realistic_nm_profile_values_do_not_trigger_rebinding(self):
        from types import SimpleNamespace
        calls=[]
        def command(args,**kwargs):
            calls.append(args)
            if '-g' not in args:return SimpleNamespace(returncode=0,stdout='',stderr='')
            field=args[args.index('-g')+1]
            ident=args[-1]
            if field=='UUID':value=self.w.AP+'\nclient'
            else:
                value={'connection.type':'802-11-wireless','802-11-wireless.ssid':'pisondemobile' if ident==self.w.AP else 'John:phone',
                       '802-11-wireless.mac-address':self.w.AP_MAC if ident==self.w.AP else self.w.CLIENT_MAC,
                       '802-11-wireless.mode':'ap' if ident==self.w.AP else 'infrastructure',
                       '802-11-wireless-security.key-mgmt':'wpa-psk'}[field]
            if args[args.index('--escape')+1]=='yes':value=value.replace('\\','\\\\').replace(':',r'\:')
            return SimpleNamespace(returncode=0,stdout=value,stderr='')
        with patch.object(self.w.subprocess,'run',side_effect=command),patch.object(self.w,'device',return_value=None),patch.object(self.w,'status',return_value={}):
            rows=self.w.profiles()
            self.assertEqual(rows[0]['mac'],self.w.AP_MAC)
            self.assertEqual(rows[1]['ssid'],'John:phone')
            self.w.ensure()
        self.assertFalse(any('modify' in command for command in calls))
    def test_saved_client_cannot_select_usb_profile(self):
        with patch.object(self.w,'device',return_value='wlan0'),patch.object(self.w,'profiles',return_value=[dict(uuid=self.w.AP,ssid='pisondemobile',mac=self.w.AP_MAC,mode='ap')]):
            with self.assertRaises(RuntimeError):self.w.handle(dict(action='saved_connect',uuid=self.w.AP))
    def test_hotspot_creation_always_on_linksys(self):
        calls=[]
        def nm(*args,**kw):calls.append(args);return ''
        with patch.object(self.w,'device',return_value='wlan0'),patch.object(self.w,'profiles',return_value=[]),patch.object(self.w,'nm',side_effect=nm),patch.object(self.w,'status',return_value={}):
            self.w.handle(dict(action='connect',ssid='iPhone',password='password123'))
        add=next(c for c in calls if c[0:2]==('connection','add'))
        self.assertIn(self.w.CLIENT_MAC,add);self.assertNotIn(self.w.AP_MAC,add)
        self.assertEqual(calls[-1][-2:],('ifname','wlan0'))
    def test_profile_guard_and_ap_recovery(self):
        rows=[dict(uuid=self.w.AP,mac='',mode='infrastructure'),dict(uuid='other',mac=self.w.AP_MAC,mode='infrastructure')];calls=[]
        with patch.object(self.w,'profiles',return_value=rows),patch.object(self.w,'device',return_value='wlan1'),patch.object(self.w,'nm',side_effect=lambda *a,**k:calls.append(a) or ''),patch.object(self.w,'status',return_value={}):self.w.ensure()
        changes=[x for x in calls if x[:2]==('connection','modify')]
        self.assertIn(self.w.AP_MAC,changes[0]);self.assertIn(self.w.CLIENT_MAC,changes[2])
        self.assertIn(('connection','up',self.w.AP),calls)
    def test_escaped_ssid_and_validation(self):
        self.assertEqual(self.w.fields(r'John\:phone:90:WPA2'),['John:phone','90','WPA2'])
        with patch.object(self.w,'device',return_value='wlan0'):
            with self.assertRaises(RuntimeError):self.w.handle(dict(action='connect',ssid='Phone',password='short'))

class BackupTests(unittest.TestCase):
    def test_checked_rotation_preserves_previous(self):
        import sqlite3
        s=load('field_test_backup','storage_maintenance.py')
        with tempfile.TemporaryDirectory() as root:
            s.DATA=Path(root)/'db.sqlite3';s.BACKUPS=Path(root)/'backups'
            db=sqlite3.connect(s.DATA);db.execute('CREATE TABLE preserved(value)');db.execute("INSERT INTO preserved VALUES ('history')");db.commit();db.close()
            s.main();s.main()
            for name in ('history-latest.sqlite3','history-previous.sqlite3'):
                db=sqlite3.connect(s.BACKUPS/name)
                self.assertEqual(db.execute('SELECT value FROM preserved').fetchone()[0],'history');db.close()

if __name__=='__main__':unittest.main(verbosity=2)
