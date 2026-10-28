"""Wi-Fi fault/recovery checks; all journal, adapter and service actions are mocked."""
import importlib.util,json,subprocess,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import nullcontext
import update_manager as updater

class WifiRecoveryTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('wifi_recovery_test',Path(__file__).with_name('wifi_control.py'))
        self.w=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.w)
        self.clock=[0.];self.r=self.w.WifiRecovery(clock=lambda:self.clock[0]);self.clock[0]=100.
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        mock=patch.object(updater,'STATE',Path(self.tmp.name));mock.start();self.addCleanup(mock.stop)
    def faults(self,role,start=0):
        return [dict(__CURSOR=role+str(start+i),MESSAGE=('brcmf_cyw_mgmt_tx: TX Auth frame operation is failed: status=10' if role=='ap' else 'device (wlan1): request-scan: could not get scan request result: Timeout was reached'),_SYSTEMD_UNIT='NetworkManager.service',_TRANSPORT='kernel' if role=='ap' else 'journal') for i in range(3)]
    def feed(self,role,start=0):self.r.ingest(self.faults(role,start),'wlan0','wlan1')
    def test_ordinary_coverage_loss_and_zero_clients_never_trigger(self):
        for text in ('ssid-not-found','CTRL-EVENT-DISCONNECTED','No stations connected','Network unreachable'):
            self.r.ingest([dict(__CURSOR=text,MESSAGE='wlan1: '+text,_SYSTEMD_UNIT='NetworkManager.service')],'wlan0','wlan1')
        self.assertIsNone(self.r.choose('wlan0','wlan1'))
    def test_repeated_specific_failures_choose_correct_adapter(self):
        for role in ('ap','client'):
            self.feed(role);self.assertEqual(self.r.choose('wlan0','wlan1'),role);self.r.record(role)
    def test_startup_grace_and_stale_events(self):
        self.clock[0]=20;self.feed('client');self.assertIsNone(self.r.choose('wlan0','wlan1'))
        self.clock[0]=150;self.r.ingest([],'wlan0','wlan1');self.assertIsNone(self.r.choose('wlan0','wlan1'))
    def test_cursor_replay_and_single_failure_cannot_trigger(self):
        for _ in range(4):self.r.ingest(self.faults('client')[:1],'wlan0','wlan1')
        self.assertIsNone(self.r.choose('wlan0','wlan1'))
    def test_unrelated_interface_and_absent_usb_cannot_trigger(self):
        rows=self.faults('client')
        for row in rows:row['MESSAGE']=row['MESSAGE'].replace('wlan1','wlan10')
        self.r.ingest(rows,'wlan0','wlan1');self.assertIsNone(self.r.choose('wlan0','wlan1'))
        self.feed('client');self.assertIsNone(self.r.choose('wlan0',None))
    def test_cooldown_and_three_attempt_hourly_limit(self):
        for i in range(3):
            self.clock[0]=100+300*i;self.feed('client',i*3);self.assertEqual(self.r.choose('wlan0','wlan1'),'client');self.r.record('client')
            self.feed('client',30+i*3);self.assertIsNone(self.r.choose('wlan0','wlan1'))
        self.clock[0]=1100;self.feed('client',100);self.assertIsNone(self.r.choose('wlan0','wlan1'))
        self.clock[0]=3800;self.feed('client',200);self.assertEqual(self.r.choose('wlan0','wlan1'),'client')
    def test_shared_restart_requires_both_faults_after_isolated_attempts(self):
        self.feed('ap');self.feed('client');self.assertEqual(self.r.choose('wlan0','wlan1'),'ap');self.r.record('ap')
        self.feed('client',10);self.assertEqual(self.r.choose('wlan0','wlan1'),'client');self.r.record('client')
        self.feed('ap',20);self.feed('client',20);self.assertEqual(self.r.choose('wlan0','wlan1'),'service');self.r.record('service')
        self.feed('ap',30);self.feed('client',30);self.assertIsNone(self.r.choose('wlan0','wlan1'))
    def test_managed_state_restored_even_when_disconnect_fails(self):
        calls=[]
        def nm(*args,**kwargs):
            calls.append(args)
            if args[-1]=='no':raise RuntimeError('simulated timeout')
            return ''
        with patch.object(self.w,'device',side_effect=lambda mac:'wlan0' if mac==self.w.AP_MAC else 'wlan1'),patch.object(self.w,'nm',side_effect=nm):
            with self.assertRaises(RuntimeError):self.w.recover_wifi('ap')
        self.assertEqual(calls,[('device','set','wlan0','managed','no'),('device','set','wlan0','managed','yes')])
    def test_upstream_recovery_uses_only_bound_saved_profile(self):
        rows=[dict(uuid=self.w.AP,mac=self.w.AP_MAC,mode='ap',ssid='pisondemobile'),dict(uuid='saved',mac=self.w.CLIENT_MAC,mode='infrastructure',ssid='Field hotspot')]
        with patch.object(self.w,'device',return_value='wlan1'),patch.object(self.w,'profiles',return_value=rows),patch.object(self.w,'forgotten',return_value=set()),patch.object(self.w,'nm',return_value='100') as nm:
            self.w.recover_wifi('client')
        self.assertIn(unittest.mock.call('connection','up','saved','ifname','wlan1',timeout=18),nm.call_args_list)
        self.assertFalse(any(c.args[:2]==('connection','add') for c in nm.call_args_list))
        self.assertFalse(any(self.w.AP in c.args for c in nm.call_args_list))
    def test_fixed_service_command_and_ap_restore(self):
        with patch.object(self.w,'device',return_value='wlan0'),patch.object(self.w.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run,patch.object(self.w,'nm') as nm:
            self.w.recover_wifi('service')
        self.assertEqual(run.call_args.args[0],['/usr/bin/systemctl','restart','wpa_supplicant.service'])
        nm.assert_called_once_with('connection','up',self.w.AP,timeout=18)
    def test_update_power_and_busy_locks_block_recovery(self):
        self.w.wifi_recovery=self.r
        with patch.object(self.w,'device',return_value='wlan1'),patch.object(updater,'locked',return_value=nullcontext()),patch.object(updater,'status',return_value={}),patch.object(updater,'power_pending',return_value=True),patch.object(self.w,'recover_wifi') as recover:
            self.w.wifi_health_tick(self.faults('client'));recover.assert_not_called()
        with patch.object(self.w,'device',return_value='wlan1'),patch.object(updater,'locked',side_effect=RuntimeError('busy')),patch.object(self.w,'recover_wifi') as recover:
            self.w.wifi_health_tick([]);recover.assert_not_called()
        self.assertEqual(self.w.WIFI_RECOVERY['adapter_attempts'],0)
    def test_tick_records_attempt_and_fixed_reason_without_raw_log(self):
        self.w.wifi_recovery=self.r
        with patch.object(self.w,'device',return_value='wlan1'),patch.object(updater,'locked',return_value=nullcontext()),patch.object(updater,'status',return_value={}),patch.object(updater,'power_pending',return_value=False),patch.object(self.w,'recover_wifi',side_effect=RuntimeError('private details')):
            self.w.wifi_health_tick(self.faults('client'))
        result=self.w.WIFI_RECOVERY
        self.assertEqual(result['adapter_attempts'],1);self.assertNotIn('private details',json.dumps(result));self.assertIn('bounded retry',result['last_error'])
    def test_journal_scope_failure_and_corrupt_rows(self):
        with patch.object(self.w.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='not json\n{}\n')) as run:
            self.assertEqual(self.w.wifi_journal(1234),[{}])
        args=run.call_args.args[0];self.assertIn('-b',args);self.assertIn('@1234',args);self.assertIn('_TRANSPORT=kernel',args)
        with patch.object(self.w.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='')):
            with self.assertRaises(RuntimeError):self.w.wifi_journal(1234)

    def test_shared_restart_hourly_cap_and_missing_adapter(self):
        self.r.record('ap');self.r.record('client')
        for number in range(2):
            self.clock[0]=1000+1000*number;self.feed('ap',number*10);self.feed('client',number*10)
            self.assertNotEqual(self.r.choose('wlan0',None),'service')
            self.assertEqual(self.r.choose('wlan0','wlan1'),'service');self.r.record('service')
        self.clock[0]=3000;self.feed('ap',90);self.feed('client',90)
        self.assertNotEqual(self.r.choose('wlan0','wlan1'),'service')

    def test_queued_or_interrupted_update_defers_without_attempt(self):
        self.w.wifi_recovery=self.r
        for state in ({'phase':'Queued','completed_at':None},{}):
            marker=updater.STATE/'pending.json'
            if not state:marker.write_text('{}')
            with patch.object(self.w,'device',return_value='wlan1'),patch.object(updater,'locked',return_value=nullcontext()),patch.object(updater,'status',return_value=state),patch.object(updater,'power_pending',return_value=False),patch.object(self.w,'recover_wifi') as recover:
                self.w.wifi_health_tick(self.faults('client'));recover.assert_not_called()
            marker.unlink(missing_ok=True)
        self.assertEqual(self.w.WIFI_RECOVERY['adapter_attempts'],0)
