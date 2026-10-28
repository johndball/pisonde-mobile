"""Retention and power controls: temporary databases and mocked system actions only."""
import importlib,sqlite3,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import test_field_ui as ui_tests

class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ui_tests.GuiTests('test_status_reads_do_not_touch_stored_history');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups);self.addCleanup(self.fixture.tearDown)
        self.m=self.fixture.m;self.f=self.fixture.f;self.client=self.fixture.client
        self.client.get('/')
        with self.client.session_transaction() as session:self.headers={'X-CSRF-Token':session['token']}
    def test_sdr_reboot_policy_requires_csrf_and_boolean(self):
        self.assertEqual(self.client.post('/api/sdr-recovery',json={'enabled':True}).status_code,403)
        self.assertEqual(self.client.post('/api/sdr-recovery',headers=self.headers,json={'enabled':'yes'}).status_code,400)
        with patch.object(self.f,'broker',return_value={'enabled':True}) as broker:
            self.assertEqual(self.client.get('/api/sdr-recovery').json,{'enabled':True})
            broker.assert_called_with('sdr_recovery')
            self.assertEqual(self.client.post('/api/sdr-recovery',headers=self.headers,json={'enabled':True}).status_code,200)
            broker.assert_called_with('sdr_recovery',enabled=True)
        self.assertIn('Reboot PiSonde if SDR recovery is exhausted',self.client.get('/device').text)

    def add(self,call='TEST',days=100,lat=38.,lon=-77.,raw='packet'):
        when=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
        ident=self.fixture.add(call=call,lat=lat,lon=lon,when=when)
        with self.m.database() as db:db.execute('UPDATE receptions SET raw_packet=? WHERE id=?',(raw,ident));count=db.execute('SELECT COUNT(*) FROM receptions').fetchone()[0]
        self.m.status['packets']=count
        return ident
    def ids(self):
        with self.m.database() as db:return [r[0] for r in db.execute('SELECT id FROM receptions ORDER BY id')]
    def policy(self,**values):
        cfg=dict(self.f.RETENTION_DEFAULT,**values);self.m.atomic_json(self.m.ROOT/'retention.json',cfg);return cfg
    def test_default_keeps_all_history(self):
        self.add(days=900);self.add(days=0);before=self.ids();self.f.retention_cycle();self.assertEqual(self.ids(),before)
        self.assertEqual(self.client.get('/api/retention').json['policy']['mode'],'forever')
    def test_all_age_choices_remove_old_rows_but_keep_newest(self):
        for days in (30,60,90,180,365):
            with self.subTest(days=days):
                with self.m.database() as db:db.execute('DELETE FROM receptions')
                old=self.add(days=days+1);recent=self.add(days=days-1);last=self.add(days=0)
                self.policy(mode='days',days=days,protect_latest=False);self.f.retention_cycle()
                self.assertNotIn(old,self.ids());self.assertEqual(self.ids(),[recent,last]);self.assertEqual(self.m.status['packets'],2)
    def test_latest_position_and_noted_target_are_protected(self):
        old=self.add('POSITION');pos=self.add('POSITION');note_old=self.add('NOTED',lat=None,lon=None);noted=self.add('NOTED',lat=None,lon=None);last=self.add('LAST',days=0)
        with self.m.database() as db:db.execute('INSERT INTO target_notes VALUES (?,?,?,?)',('RS41','NOTED','Trail access','Recovery planned'))
        self.policy(mode='days',days=30);self.f.retention_cycle()
        self.assertEqual(self.ids(),[pos,noted,last]);self.assertNotIn(old,self.ids());self.assertNotIn(note_old,self.ids())
        with self.m.database() as db:self.assertEqual(db.execute('SELECT note FROM target_notes').fetchone()[0],'Trail access')
    def test_linked_upload_payloads_removed_atomically(self):
        old=self.add();last=self.add(days=0)
        with self.m.database() as db:
            for ident in (old,last):db.execute('INSERT INTO upload_jobs(reception_id,destination,payload,status,created_utc) VALUES (?,?,?,?,?)',(ident,'sondehub','{}','expired',datetime.now(timezone.utc).isoformat()))
        self.policy(mode='days',days=30,protect_latest=False);self.f.retention_cycle()
        with self.m.database() as db:self.assertEqual([r[0] for r in db.execute('SELECT reception_id FROM upload_jobs')],[last])
    def test_deletion_invalidates_dashboard_target_cache(self):
        self.add('OLD');self.add('NEW',days=0);self.fixture.status()
        self.policy(mode='days',days=30,protect_latest=False);self.f.retention_cycle();state=self.fixture.status()
        self.assertEqual({r['callsign'] for r in state['ui_targets']},{'NEW'});self.assertGreater(state['retention_generation'],0)
    def test_low_storage_above_threshold_does_nothing(self):
        self.add();self.add(days=0);before=self.ids();self.policy(mode='low_space',protect_latest=False)
        with patch.object(self.f,'free_percent',return_value=11),patch.object(self.f,'compact_history') as compact:self.f.retention_cycle()
        self.assertEqual(self.ids(),before);compact.assert_not_called()
    def test_low_storage_cleans_oldest_and_compacts_real_database(self):
        for _ in range(20):self.add(raw='x'*20000)
        last=self.add(days=0)
        with self.m.database() as db:db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before=self.m.DB.stat().st_size
        self.policy(mode='low_space',protect_latest=False)
        disk=SimpleNamespace(total=10*1024**3,free=512*1024**2,used=0)
        with patch.object(self.f,'free_percent',return_value=5),patch.object(self.f.shutil,'disk_usage',return_value=disk):self.f.retention_cycle()
        self.assertEqual(self.ids(),[last]);self.assertLess(self.m.DB.stat().st_size,before)
        with self.m.database() as db:self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
    def test_low_storage_insufficient_working_space_preserves_history(self):
        self.add();self.add(days=0);before=self.ids();self.policy(mode='low_space',protect_latest=False)
        with patch.object(self.f,'free_percent',return_value=1),patch.object(self.f.shutil,'disk_usage',return_value=SimpleNamespace(total=1000,free=1,used=999)):self.f.retention_cycle()
        self.assertEqual(self.ids(),before);self.assertIn('Free other storage',self.f.retention_state['last_error'])
    def test_low_storage_keeps_recent_upload_window(self):
        old=self.add();recent=self.add(days=0);last=self.add(days=100);self.policy(mode='low_space',protect_latest=False)
        disk=SimpleNamespace(total=10*1024**3,free=512*1024**2,used=0)
        with patch.object(self.f,'free_percent',return_value=5),patch.object(self.f.shutil,'disk_usage',return_value=disk):self.f.retention_cycle()
        self.assertEqual(self.ids(),[recent,last]);self.assertNotIn(old,self.ids())
    def test_batch_bound_and_highest_id_prevent_identity_reuse(self):
        when=(datetime.now(timezone.utc)-timedelta(days=100)).isoformat()
        with self.m.database() as db:db.executemany('INSERT INTO receptions(received_utc,protocol,frequency_mhz,callsign,latitude,longitude,altitude_m,raw_packet) VALUES (?,?,?,?,?,?,?,?)',[(when,'RS41',403.,'MANY',None,None,None,'raw')]*10005)
        self.m.status['packets']=10005;self.policy(mode='days',days=30,protect_latest=False);self.f.retention_cycle()
        self.assertEqual(len(self.ids()),5);highest=max(self.ids());self.f.retention_cycle();self.assertEqual(self.ids(),[highest])
        self.assertGreater(self.add(days=0),highest)
    def test_api_requires_csrf_and_explicit_confirmation(self):
        cfg=dict(self.f.RETENTION_DEFAULT,mode='days',days=30)
        self.assertEqual(self.client.post('/api/retention',json={'policy':cfg,'confirmed':True}).status_code,403)
        self.assertEqual(self.client.post('/api/retention',headers=self.headers,json={'policy':cfg,'confirmed':False}).status_code,400)
        self.assertEqual(self.client.post('/api/retention',headers=self.headers,json={'policy':cfg,'confirmed':True}).status_code,200)
        self.assertEqual(self.f.retention_policy(),cfg);self.assertTrue(self.f.retention_wake.is_set())
    def test_invalid_policy_rejected(self):
        for bad in (dict(self.f.RETENTION_DEFAULT,mode='all'),dict(self.f.RETENTION_DEFAULT,days=1),dict(self.f.RETENTION_DEFAULT,days=True),dict(self.f.RETENTION_DEFAULT,threshold=-1),dict(self.f.RETENTION_DEFAULT,protect_latest='yes')):
            response=self.client.post('/api/retention',headers=self.headers,json={'policy':bad,'confirmed':True});self.assertEqual(response.status_code,400)
    def test_backups_and_unrelated_files_are_not_removed(self):
        sentinel=self.m.ROOT/'saved-export.csv';sentinel.write_text('keep')
        self.add();self.add(days=0);self.policy(mode='days',days=30,protect_latest=False);self.f.retention_cycle();self.assertEqual(sentinel.read_text(),'keep')
    def test_cleanup_failure_rolls_back_receptions_and_upload_payloads(self):
        from contextlib import contextmanager
        old=self.add();last=self.add(days=0)
        with self.m.database() as db:db.execute('INSERT INTO upload_jobs(reception_id,destination,payload,status,created_utc) VALUES (?,?,?,?,?)',(old,'sondehub','{}','expired',datetime.now(timezone.utc).isoformat()))
        self.policy(mode='days',days=30,protect_latest=False);real_database=self.m.database
        @contextmanager
        def failing_database():
            with real_database() as db:
                class Proxy:
                    def __getattr__(self,name):return getattr(db,name)
                    def execute(self,sql,*args):
                        if sql.startswith('DELETE FROM receptions'):raise sqlite3.OperationalError('simulated interruption')
                        return db.execute(sql,*args)
                yield Proxy()
        with patch.object(self.m,'database',failing_database):
            with self.assertRaises(sqlite3.OperationalError):self.f.retention_cycle()
        self.assertEqual(self.ids(),[old,last]);self.assertEqual(self.m.status['packets'],2)
        with self.m.database() as db:self.assertEqual(db.execute('SELECT reception_id FROM upload_jobs').fetchone()[0],old)
    def test_power_api_requires_csrf_and_rejects_parameters(self):
        self.assertEqual(self.client.post('/api/power',json={'operation':'shutdown'}).status_code,403)
        with patch.object(self.f,'broker',return_value={'queued':'restart'}) as broker:
            r=self.client.post('/api/power',headers=self.headers,json={'operation':'restart'});self.assertEqual(r.status_code,200);broker.assert_called_once_with('power',operation='restart')
            self.assertEqual(self.client.post('/api/power',headers=self.headers,json={'operation':'shutdown','command':'anything'}).status_code,400)
            self.assertEqual(broker.call_count,1)
    def test_theme_navigation_and_dedicated_device_page(self):
        for path in ('/','/uploads','/wifi','/history','/updates','/help'):
            page=self.client.get(path).text
            self.assertNotIn('data-power="restart"',page);self.assertNotIn('data-power="shutdown"',page);self.assertIn('--blue:#155eab',page);self.assertIn('href="/device"',page)
        device=self.client.get('/device').text
        self.assertIn('data-power="restart"',device);self.assertIn('data-power="shutdown"',device);self.assertIn('aria-current="page">Device controls',device)
        home=self.client.get('/').text
        self.assertLess(home.index('href="/updates"'),home.index('href="/device"'));self.assertLess(home.index('href="/device"'),home.index('href="/help"'))
        uploads=self.client.get('/uploads').text
        for value in ('30 days','60 days','90 days','180 days','365 days','10%','Protect latest location'):self.assertIn(value,uploads)

    def test_browser_scripts_keep_edits_and_confirm_power(self):
        import json,re,shutil,subprocess
        if not shutil.which('node'):self.skipTest('Node unavailable')
        page=self.client.get('/uploads').text+self.client.get('/device').text;scripts=re.findall(r'<script>(.*?)</script>',page,re.S)
        javascript=r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8')),elements={},requests=[];
function element(id){return elements[id]??=({value:'',checked:false,disabled:false,hidden:false,textContent:'',dataset:{}});}
element('retention-mode').value='forever';
const power=[{dataset:{power:'restart'},disabled:false},{dataset:{power:'shutdown'},disabled:false}];
let allowConfirm=true;const state=input.status;
const context=vm.createContext({document:{getElementById:element,querySelectorAll:()=>power},AbortController,setTimeout,clearTimeout,setInterval:()=>{},confirm:()=>allowConfirm,fetch:async(url,options)=>{
 if(options?.method==='POST'){const body=JSON.parse(options.body);requests.push({url,body});if(url==='/api/retention'){state.retention.policy=body.policy;return {ok:true,json:async()=>state.retention};}return {ok:true,json:async()=>({queued:body.operation})};}
 return {ok:true,json:async()=>state};
}});
(async()=>{
 for(const script of input.scripts)vm.runInContext(script,context);
 await new Promise(setImmediate);
 assert.equal(element('retention-mode').value,'forever');assert.equal(element('retention-days-label').hidden,true);
 element('retention-mode').value='days';element('retention-mode').onchange();element('retention-days').value='30';
 await vm.runInContext('status()',context);assert.equal(element('retention-days').value,'30');assert.equal(element('retention-mode').value,'days');
 await element('retention-save').onclick();assert.equal(requests[0].url,'/api/retention');assert.equal(requests[0].body.policy.days,30);assert.equal(requests[0].body.confirmed,true);assert.match(element('retention-feedback').textContent,/saved/);
 allowConfirm=false;await power[0].onclick();assert.equal(requests.length,1);
 allowConfirm=true;await power[0].onclick();assert.equal(requests[1].url,'/api/power');assert.deepEqual(requests[1].body,{operation:'restart'});assert.ok(power.every(b=>b.disabled));
 console.log('PASS: retention edits survive polling, policy save and power confirmation use expected APIs');
})().catch(e=>{console.error(e);process.exit(1)});
'''
        result=subprocess.run(['node','-e',javascript],input=json.dumps(dict(scripts=scripts,status=self.fixture.status())),text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

class PowerBrokerTests(unittest.TestCase):
    def setUp(self):
        import update_manager as u,wifi_control as w
        self.u=u;self.w=w;self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name)
        for name,path in {'STATE':root/'state','POWER_PENDING':root/'run'/'power.json','APP':root/'app'}.items():
            mock=patch.object(u,name,path);mock.start();self.addCleanup(mock.stop)
        u.APP.mkdir();(u.APP/'mobile.py').write_text("VERSION='0.3.17'\n")
        mock=patch.object(u,'command');self.command=mock.start();self.addCleanup(mock.stop)
    def test_only_fixed_restart_and_shutdown_commands_are_queued(self):
        for operation,target in (('restart','reboot'),('shutdown','poweroff')):
            self.u.POWER_PENDING.unlink(missing_ok=True)
            result=self.w.power_action(dict(action='power',operation=operation));args=self.command.call_args.args[0]
            self.assertEqual(args[0],'/usr/bin/systemd-run');self.assertIn('--on-active=5s',args);self.assertEqual(args[-2:],['/usr/bin/systemctl',target]);self.assertEqual(result['queued'],operation)
    def test_unknown_actions_and_extra_parameters_are_rejected(self):
        for value in ({'action':'power','operation':'shell'},{'action':'power','operation':'restart','device':'anything'}):
            with self.assertRaises(RuntimeError):self.w.power_action(value)
        self.command.assert_not_called()
    def test_nonroot_request_rejected(self):
        with patch.object(self.w.os,'geteuid',return_value=999):
            with self.assertRaises(RuntimeError):self.w.power_action(dict(action='power',operation='restart'))
        self.command.assert_not_called()
    def test_duplicate_power_actions_rejected(self):
        self.w.power_action(dict(action='power',operation='restart'))
        with self.assertRaises(RuntimeError):self.w.power_action(dict(action='power',operation='shutdown'))
        self.assertEqual(self.command.call_count,1)
    def test_active_update_lock_blocks_power(self):
        with self.u.locked():
            with self.assertRaises(RuntimeError):self.w.power_action(dict(action='power',operation='restart'))
        self.command.assert_not_called()
    def test_queued_or_interrupted_update_blocks_power(self):
        self.u.put(self.u.STATE/'status.json',dict(phase='Queued',queued_at=self.u.now(),started_at=None))
        with self.assertRaises(RuntimeError):self.w.power_action(dict(action='power',operation='restart'))
        (self.u.STATE/'status.json').unlink();self.u.put(self.u.STATE/'pending.json',{'backup':'unfinished'})
        with self.assertRaises(RuntimeError):self.w.power_action(dict(action='power',operation='restart'))
        self.command.assert_not_called()
    def test_pending_power_blocks_update_installation(self):
        self.w.power_action(dict(action='power',operation='restart'))
        with patch.object(self.u,'catalog') as catalog:
            with self.assertRaises(RuntimeError):self.u.run('install')
        catalog.assert_not_called()
