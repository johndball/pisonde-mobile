import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('mobile',Path(__file__).with_name('mobile.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old=(m.ROOT,m.DB,m.CONFIGFILE,m.RUNTIME,m.LOGROOT,m.config,m.revision)
        m.ROOT=Path(self.tmp.name);m.DB=m.ROOT/'receptions.sqlite3'
        m.CONFIGFILE=m.ROOT/'scanner.json';m.RUNTIME=m.ROOT/'runtime';m.LOGROOT=m.ROOT/'logs'
        m.config=dict(m.DEFAULT);m.revision=0;m.skip_once=None
        m.shutdown_event.clear();m.setup();m.app.secret_key='offline-test-only'
    def tearDown(self):
        m.shutdown_event.set()
        m.ROOT,m.DB,m.CONFIGFILE,m.RUNTIME,m.LOGROOT,m.config,m.revision=self.old
        self.tmp.cleanup()
    def test_startup_clears_saved_manual_lock(self):
        saved=dict(m.DEFAULT,profile='MANUAL',manual_mode='HORUS',manual_mhz=434.210,gain=29.7)
        m.atomic_json(m.CONFIGFILE,saved);m.config=dict(m.DEFAULT);m.setup()
        self.assertEqual(m.config['profile'],'ALL')
        self.assertEqual(m.config['manual_mode'],'HORUS')
        self.assertEqual(m.config['manual_mhz'],434.210)
        self.assertEqual(m.config['gain'],29.7)
        self.assertEqual(json.loads(m.CONFIGFILE.read_text())['profile'],'ALL')
        m.config=dict(m.DEFAULT);m.setup();self.assertEqual(m.config['profile'],'ALL')

    def test_startup_preserves_selected_scan_profile(self):
        for profile in ('ALL','APRS','HORUS','HORUS_WIDE','DFM','RS41'):
            with self.subTest(profile=profile):
                m.atomic_json(m.CONFIGFILE,dict(m.DEFAULT,profile=profile))
                m.config=dict(m.DEFAULT);m.setup()
                self.assertEqual(m.config['profile'],profile)

    def test_startup_resumes_from_saved_stop(self):
        m.atomic_json(m.CONFIGFILE,dict(m.DEFAULT,profile='STOP'))
        m.config=dict(m.DEFAULT);m.setup();self.assertEqual(m.config['profile'],'ALL')

    def test_aprs_storage_and_map(self):
        p=m.parse_packet('[0.2] N0TEST>APRS:!3800.00N/07800.00W>TEST','APRS')
        self.assertEqual((p['latitude'],p['longitude']),(38.,-78.))
        m.store_packet(p,'APRS',144390000)
        with m.database() as db:
            row=db.execute('SELECT * FROM receptions').fetchone()
            self.assertEqual(row['frequency_mhz'],144.39)
            self.assertEqual(row['callsign'],'N0TEST')
        self.assertIsNone(m.parse_packet('N0TEST audio level = 71','APRS'))
    def test_status_and_weather_round_trip(self):
        for mode,line in [('APRS','[0.2] N0TEST>APRS:>TEST'),
                          ('DFM',json.dumps({'id':'DFM-123','lat':38.1,'lon':-78.2,'alt':100})),
                          ('RS41',json.dumps({'id':'X1234567','lat':38.2,'lon':-78.3,'alt':200}))]:
            p=m.parse_packet(line,mode);self.assertIsNotNone(p)
            m.store_packet(p,mode,144390000 if mode=='APRS' else 404000000)
        with m.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM receptions').fetchone()[0],3)
        self.assertIsNone(m.parse_packet('{"lat":38,"lon":-78}','DFM'))
        self.assertIsNone(m.parse_packet('DFM9: 0.9','DFM'))
        p=m.parse_packet('{"id":"DFM-X","lat":999,"lon":0,"alt":NaN}','DFM')
        m.store_packet(p,'DFM',404000000)
        with m.database() as db:
            row=db.execute('SELECT latitude,altitude_m FROM receptions ORDER BY id DESC').fetchone()
            self.assertEqual(tuple(row),(None,None))
    def test_horus_real_crc(self):
        import binascii
        raw=struct.pack('<BH3sffHBBbB',0,42,bytes([12,30,0]),38.0,-78.0,1000,10,8,20,150)
        raw+=struct.pack('<H',binascii.crc_hqx(raw,0xffff))
        p=m.parse_packet(raw.hex(),'HORUS')
        self.assertIsNotNone(p)
        self.assertEqual(p['latitude'],38.0)
        m.store_packet(p,'HORUS',432501000)
        broken=bytearray(raw);broken[4]^=0x20
        self.assertIsNone(m.parse_packet(broken.hex(),'HORUS'))
    def test_spectrum_peak_and_noise_rejection(self):
        powers=[-70.]*160;powers[80]=-51.
        text='2026-10-21, 12:00:00, 403920000, 404080000, 1000, 100, '+', '.join(map(str,powers))
        peaks=m.spectrum_candidates(text,m.config)
        self.assertEqual(peaks[0]['hz'],404000000)
        noise=text.replace('-51.0','-70.0')
        self.assertEqual(m.spectrum_candidates(noise,m.config),[])
    def test_exact_launch_channels_and_iq(self):
        self.assertEqual(m.APRS_HZ,[144390000])
        self.assertEqual(m.HORUS_HZ,[432501000,434200000,434210000])
        for hz in m.HORUS_HZ:
            commands=m.commands_for('HORUS',hz,m.config)
            self.assertIn(str(hz-1000),commands[0]);self.assertIn('-q',commands[1])
        self.assertIn('15000',m.commands_for('DFM',404000000,m.config)[0])
        self.assertIn('-c',m.commands_for('DETECT',404000000,m.config)[-1])
    def test_all_mode_round_robin(self):
        seen=[]
        def listen(mode,hz,*args,**kwargs): seen.append((mode,hz))
        def survey(*args):
            seen.append(('SURVEY',None));return [{'hz':404000000,'measured_hz':404000000}]
        def classify(*args):return 'DFM'
        def final_listen(mode,hz,*args,**kwargs):
            listen(mode,hz)
            if mode=='DFM':m.shutdown_event.set()
        with patch.object(m,'listen',final_listen),patch.object(m,'survey',survey),patch.object(m,'classify',classify):
            m.scanner()
        self.assertEqual(seen,[('APRS',144390000),('HORUS',432501000),('HORUS',434200000),
                              ('HORUS',434210000),('SURVEY',None),('DFM',404000000)])
    def test_protocol_filter(self):
        m.config['profile']='RS41';called=[]
        def classify(*a):m.shutdown_event.set();return 'DFM'
        with patch.object(m,'survey',return_value=[dict(hz=404000000,measured_hz=404000000)]), \
             patch.object(m,'classify',classify),patch.object(m,'listen',side_effect=lambda *a:called.append(a)):
            m.scanner()
        self.assertEqual(called,[])
    def test_hold_timer_extends_after_each_packet(self):
        # Exercise actual listen loop with simulated packet arrival times, not SDR hardware.
        clock=[0.]; arrivals=iter([(1.,'[0.2] N0TEST>APRS:>ONE'),(61.,'[0.2] N0TEST>APRS:>TWO')])
        class Output:
            def get(self,timeout):
                try:
                    now,line=next(arrivals);clock[0]=now;return line
                except StopIteration:
                    clock[0]+=10;raise m.queue.Empty
        class Pipe:
            def __init__(self,*a):self.output=Output()
            def check_flow(self):return True
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def ended(self):return False
            def failed(self):return False
        with patch.object(m,'Pipeline',Pipe),patch.object(m.time,'monotonic',side_effect=lambda:clock[0]):
            count=m.listen('APRS',144390000,8,m.config,0)
        self.assertEqual(count,2)
        self.assertGreaterEqual(clock[0],126.)
        self.assertLess(clock[0],140.)
    def test_pipeline_releases_children(self):
        commands=[[__import__('sys').executable,'-u','-c','import time; print("ready",flush=True); time.sleep(30)']]
        with m.Pipeline(commands) as pipe:
            self.assertEqual(pipe.output.get(timeout=3),'ready');children=list(pipe.processes)
        self.assertTrue(all(p.poll() is not None for p in children))
    def test_controls_csrf_and_persistence(self):
        client=m.app.test_client()
        self.assertEqual(client.get('/').status_code,200)
        self.assertEqual(client.post('/api/control',json={'profile':'DFM'}).status_code,403)
        with client.session_transaction() as session:token=session['token']
        headers={'X-CSRF-Token':token}
        self.assertEqual(client.post('/api/control',json={'profile':'DFM'},headers=headers).status_code,200)
        self.assertEqual(json.loads(m.CONFIGFILE.read_text())['profile'],'DFM')
        self.assertEqual(client.post('/api/control',json={'profile':'MANUAL','manual_mode':'DFM','manual_mhz':144.39},headers=headers).status_code,400)
        self.assertEqual(client.get('/api/status').json['config']['profile'],'DFM')
        self.assertEqual(client.get('/history.csv').status_code,200)
    def test_existing_history_preserved(self):
        with m.database() as db:
            db.execute("INSERT INTO receptions(received_utc,protocol,frequency_mhz,callsign,raw_packet) VALUES ('old','APRS',144.39,'OLD','old')")
        m.setup()
        with m.database() as db:
            self.assertEqual(db.execute('SELECT callsign FROM receptions').fetchone()[0],'OLD')
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
    def test_wide_horus_peak_grid(self):
        powers=[-70.]*160;powers[80]=-50.
        # Preserve 1 kHz placement instead of rounding launch .501 to .500.
        text='2026-10-21, 12:00:00, 432421000, 432581000, 1000, 100, '+', '.join(map(str,powers))
        peaks=m.spectrum_candidates(text,m.config,band='horus')
        self.assertEqual(peaks[0]['hz'],432501000)
        self.assertEqual(m.spectrum_candidates(text,m.config),[])
    def test_wide_horus_schedule_and_offset(self):
        m.config['profile']='HORUS_WIDE';seen=[]
        def listen(mode,hz,dwell,c,*args,**kwargs):
            seen.append((mode,hz,c.get('horus_offset_hz')))
            if hz==434650000:m.shutdown_event.set()
        def survey(c,rev,band='weather'):
            self.assertEqual(band,'horus')
            self.assertEqual((c['horus_min_mhz'],c['horus_max_mhz']),(432.,435.))
            return [dict(hz=434650000,measured_hz=434650000)]
        with patch.object(m,'listen',listen),patch.object(m,'survey',survey):m.scanner()
        self.assertEqual(seen,[('HORUS',432501000,None),('HORUS',434200000,None),
                               ('HORUS',434210000,None),('HORUS',434650000,5000)])
        command=m.commands_for('HORUS',434650000,dict(m.config,horus_offset_hz=5000))
        self.assertIn('434645000',command[0]);self.assertIn('10000',command[1])
    def test_wide_profile_persists(self):
        client=m.app.test_client();client.get('/')
        with client.session_transaction() as session:token=session['token']
        response=client.post('/api/control',json={'profile':'HORUS_WIDE'},headers={'X-CSRF-Token':token})
        self.assertEqual(response.status_code,200)
        m.setup()
        self.assertEqual(m.config['profile'],'HORUS_WIDE')
        self.assertIn('Horus 432.501, 434.200, 434.210',client.get('/').data.decode())
        self.assertIn('Horus — wide search 432–435 MHz',client.get('/').data.decode())
    def test_keep_tracking_preserves_wide_receiver(self):
        client=m.app.test_client();client.get('/')
        with client.session_transaction() as session:token=session['token']
        m.config['profile']='HORUS_WIDE'
        m.status.update(mode='HORUS',frequency_mhz=434.650)
        response=client.post('/api/control',json={'action':'keep'},headers={'X-CSRF-Token':token})
        self.assertEqual(response.status_code,200)
        self.assertEqual(m.config['profile'],'MANUAL')
        self.assertEqual(m.config['manual_mhz'],434.650)
        self.assertEqual(m.config['manual_horus_offset_hz'],5000)
        response=client.post('/api/control',json={'action':'resume'},headers={'X-CSRF-Token':token})
        self.assertEqual(response.status_code,200)
        self.assertEqual(m.config['profile'],'ALL')
    def test_web_script_render(self):
        client=m.app.test_client();body=client.get('/').data.decode()
        script=body.split('<script>')[1].split('</script>')[0]
        path=Path(self.tmp.name)/'ui.js';path.write_text(script)
        if shutil_which:=__import__('shutil').which('node'):
            subprocess.run([shutil_which,'--check',str(path)],check=True,capture_output=True)
        self.assertNotIn('Listen / Hold',body)
        self.assertIn('Auto — all modes',body)
        self.assertIn('Lock to Frequency',body)
        self.assertNotIn('Keep tracking',body)
        self.assertIn('432.501',body)

if __name__=='__main__':unittest.main(verbosity=2)
