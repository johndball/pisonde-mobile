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
        mock=patch.object(self.w,'client_link',return_value={'client_connected':False});mock.start();self.addCleanup(mock.stop)
        self.clock=[0.];self.r=self.w.WifiRecovery(clock=lambda:self.clock[0]);self.clock[0]=100.
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        mock=patch.object(updater,'STATE',Path(self.tmp.name));mock.start();self.addCleanup(mock.stop)
    def test_gateway_stall_requires_known_gateway_and_ninety_seconds(self):
        link=dict(client_connected=True,client_uuid='saved',gateway='192.0.2.1')
        with patch.object(self.w,'nm',return_value='Field hotspot'):
            for t in (100,130,160,190):
                self.clock[0]=t;self.r.observe_gateway(link,'failed');self.r.observe_link(link)
            self.assertIsNone(self.r.choose('wlan0','wlan1'))
            self.r.observe_gateway(link,'reachable')
            for t in (200,230,260):
                self.clock[0]=t;self.r.observe_gateway(link,'failed');self.r.observe_link(link)
                self.assertIsNone(self.r.choose('wlan0','wlan1'))
            self.clock[0]=290;self.r.observe_gateway(link,'failed');self.r.observe_link(link)
            self.assertEqual(self.r.choose('wlan0','wlan1'),'client');self.r.record('client')
            for t in range(320,591,30):
                self.clock[0]=t;self.r.observe_gateway(link,'failed');self.r.observe_link(link)
            self.assertEqual(self.r.choose('wlan0','wlan1'),'usb')
            self.r.observe_gateway(link,'reachable');self.r.observe_link(link)
            self.assertFalse(self.r.client_stage);self.assertIsNone(self.r.choose('wlan0','wlan1'))
    def test_gateway_unknown_stale_and_coverage_loss_do_not_trigger(self):
        link=dict(client_connected=True,client_uuid='saved',gateway='192.0.2.1')
        self.r.observe_gateway(link,'reachable')
        for health in ('unverified','unavailable'):
            for t in (200,230,260,290):
                self.clock[0]=t;self.r.observe_gateway(link,health)
                self.assertFalse(self.r.link_failed)
        link['client_connected']=False
        self.r.observe_gateway(link,'failed');self.assertFalse(self.r.link_failed)
        link.update(client_connected=True,gateway='192.0.2.2')
        for t in (300,330,360,390):
            self.clock[0]=t;self.r.observe_gateway(link,'failed');self.assertFalse(self.r.link_failed)
    def test_blocked_ping_with_reachable_arp_is_healthy(self):
        row=json.dumps([dict(dst='192.0.2.1',dev='wlan1',state=['REACHABLE'])])
        def run(args,**kwargs):return SimpleNamespace(returncode=1 if args[0]=='ping' else 0,stdout=row)
        with patch.object(self.w.subprocess,'run',side_effect=run) as command:
            self.assertEqual(self.w.gateway_health('wlan1',dict(client_connected=True,gateway='192.0.2.1')),'reachable')
        self.assertTrue(all(c.kwargs['timeout']<=2 for c in command.call_args_list))
    def test_failed_arp_not_ping_failure_is_required(self):
        link=dict(client_connected=True,gateway='192.0.2.1')
        for state,expected in (('FAILED','failed'),('STALE','unverified'),('INCOMPLETE','unverified')):
            row=json.dumps([dict(dst='192.0.2.1',dev='wlan1',state=[state])])
            with patch.object(self.w.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=row)):
                self.assertEqual(self.w.gateway_health('wlan1',link),expected)
        with patch.object(self.w.subprocess,'run') as run:
            self.assertEqual(self.w.gateway_health('wlan1',dict(client_connected=False)),'unavailable');run.assert_not_called()
    def test_recovery_history_is_bounded_and_survives_broker_restart(self):
        with patch.object(updater,'boot_id',return_value='test-boot'):
            for _ in range(65):self.w.recovery_record('attempt')
            data=updater.read(updater.STATE/'wifi-recovery.json')
            self.assertEqual(len(data['events']),60)
            self.assertEqual(data['events'][-1]['boot_id'],'test-boot')
            self.assertNotIn('ssid',json.dumps(data));self.assertNotIn(self.w.CLIENT_MAC,json.dumps(data))
            self.w.recovery_record('broker_started')
            self.assertEqual(updater.read(updater.STATE/'wifi-recovery.json')['events'][-1]['event'],'broker_started')

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
            self.clock[0]=100+300*i;self.feed('ap',i*3);self.assertEqual(self.r.choose('wlan0','wlan1'),'ap');self.r.record('ap')
            self.feed('ap',30+i*3);self.assertIsNone(self.r.choose('wlan0','wlan1'))
        self.clock[0]=1100;self.feed('ap',100);self.assertIsNone(self.r.choose('wlan0','wlan1'))
        self.clock[0]=3800;self.feed('ap',200);self.assertEqual(self.r.choose('wlan0','wlan1'),'ap')
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

    def auth_rows(self,times,ssid='Field hotspot',start=0,reason='CONN_FAILED'):
        return [dict(__CURSOR='auth'+str(start+i),__MONOTONIC_TIMESTAMP=str(int(t*1000000)),_SYSTEMD_UNIT='wpa_supplicant.service',MESSAGE=f'wlan1: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid="{ssid}" auth_failures=1 duration=10 reason={reason}') for i,t in enumerate(times)]
    def test_sustained_auth_failure_requires_previously_working_network(self):
        self.clock[0]=170
        self.r.ingest(self.auth_rows([100,130,165]),'wlan0','wlan1');self.assertIsNone(self.r.choose('wlan0','wlan1'))
        with patch.object(self.w,'nm',return_value='Field hotspot'):
            self.r.observe_link({'client_connected':True,'client_uuid':'saved'})
        self.r.observe_link({'client_connected':False})
        self.r.ingest(self.auth_rows([100,130,165],start=10),'wlan0','wlan1')
        self.assertEqual(self.r.choose('wlan0','wlan1'),'client')
    def test_wrong_key_other_network_and_short_auth_burst_are_ignored(self):
        self.r.known_networks.add('Field hotspot');self.clock[0]=170
        for rows in (self.auth_rows([100,130,165],reason='WRONG_KEY'),self.auth_rows([100,130,165],ssid='Unknown network',start=10),self.auth_rows([150,155,160],start=20)):
            self.r.ingest(rows,'wlan0','wlan1');self.assertIsNone(self.r.choose('wlan0','wlan1'))
    def test_auth_cursor_replay_cannot_inflate_failure_count(self):
        self.r.known_networks.add('Field hotspot');self.clock[0]=170
        rows=self.auth_rows([100,165])
        self.r.ingest(rows,'wlan0','wlan1');self.r.ingest(rows,'wlan0','wlan1')
        self.assertIsNone(self.r.choose('wlan0','wlan1'))
    def test_upstream_escalation_and_two_usb_attempt_hourly_budget(self):
        self.feed('client');self.assertEqual(self.r.choose('wlan0','wlan1'),'client');self.r.record('client')
        self.clock[0]=399;self.feed('client',10);self.assertIsNone(self.r.choose('wlan0','wlan1'))
        self.clock[0]=400;self.assertEqual(self.r.choose('wlan0','wlan1'),'usb');self.r.record('usb')
        self.clock[0]=999;self.feed('client',20);self.assertIsNone(self.r.choose('wlan0','wlan1'))
        self.clock[0]=1000;self.assertEqual(self.r.choose('wlan0','wlan1'),'usb');self.r.record('usb')
        self.clock[0]=1700;self.feed('client',30);self.assertIsNone(self.r.choose('wlan0','wlan1'));self.assertTrue(self.r.limited)
    def test_healthy_link_clears_stale_faults_and_escalation(self):
        self.feed('client');self.r.record('client');self.feed('client',10)
        with patch.object(self.w,'nm',return_value='Field hotspot'):
            self.r.observe_link({'client_connected':True,'client_uuid':'saved'})
        self.assertFalse(self.r.client_stage);self.assertIsNone(self.r.choose('wlan0','wlan1'))
    def test_expired_journal_entry_is_not_a_new_fault(self):
        rows=self.faults('client')
        for row in rows:row['__MONOTONIC_TIMESTAMP']='1000000'
        self.clock[0]=200;self.r.ingest(rows,'wlan0','wlan1');self.assertIsNone(self.r.choose('wlan0','wlan1'))
    def test_usb_reset_defers_during_update_without_consuming_budget(self):
        self.w.wifi_recovery=self.r;self.r.record('client');self.clock[0]=500
        with patch.object(self.w,'device',return_value='wlan1'),patch.object(updater,'locked',return_value=nullcontext()),patch.object(updater,'status',return_value={'phase':'Queued'}),patch.object(updater,'power_pending',return_value=False),patch.object(self.w,'request_client_usb_reset') as reset:
            self.w.wifi_health_tick(self.faults('client'));reset.assert_not_called()
        self.assertEqual(len(self.r.attempts['usb']),0)
    def test_usb_recovery_is_fixed_target_and_restores_management_after_failure(self):
        with patch.object(self.w,'device',return_value='wlan1'),patch.object(self.w,'nm') as nm,patch.object(self.w,'request_client_usb_reset',side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):self.w.recover_wifi('usb')
        self.assertEqual(nm.call_args_list,[unittest.mock.call('device','set','wlan1','managed','no'),unittest.mock.call('device','set','wlan1','managed','yes')])
    def test_link_status_requires_completed_activation_ipv4_and_gateway(self):
        # Use the real status parser, undoing the class's hardware-isolation patch.
        import importlib.util
        spec=importlib.util.spec_from_file_location('wifi_status_test',Path(__file__).with_name('wifi_control.py'));w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
        prefix='GENERAL.CONNECTION:PiSonde hotspot Field\\:phone\nGENERAL.CON-UUID:saved\n'
        for raw,connected,note in (('GENERAL.STATE:50 (connecting (configuring))\n',False,'Connecting'),('GENERAL.STATE:100 (connected)\nIP4.ADDRESS[1]:10.0.0.2/24\n',False,'waiting'),('GENERAL.STATE:100 (connected)\nIP4.ADDRESS[1]:10.0.0.2/24\nIP4.GATEWAY:10.0.0.1\n',True,'Internet unverified')):
            with patch.object(w,'nm',return_value=prefix+raw):result=w.client_link('wlan1')
            self.assertEqual(result['client_connected'],connected)
            self.assertEqual(result['hotspot'],'PiSonde hotspot Field:phone' if connected else '')
            self.assertIn(note,result['client_note'])

class UsbWifiTargetTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('usb_wifi_test',Path(__file__).with_name('wifi_control.py'));self.w=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.w)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        self.net=root/'net';self.usb=root/'usb';self.dev=root/'dev';self.net.mkdir();self.usb.mkdir();self.dev.mkdir()
        for name,value in (('NET_ROOT',self.net),('USB_ROOT',self.usb),('USB_DEV_ROOT',self.dev)):
            mock=patch.object(self.w,name,value);mock.start();self.addCleanup(mock.stop)
        self.target=self.usb/'1-3';self.target.mkdir();interface=self.target/'1-3:1.0';interface.mkdir()
        self.network=self.net/'wlan1';self.network.mkdir();(self.network/'address').write_text(self.w.CLIENT_MAC);(self.network/'device').symlink_to(interface,target_is_directory=True)
        for name,value in dict(idVendor='13b1',idProduct='003e',bDeviceClass='00',busnum='1',devnum='3').items():(self.target/name).write_text(value)
        (self.dev/'001').mkdir();self.node=self.dev/'001/003';self.node.write_bytes(b'')
    def test_target_uses_configured_mac_and_ae6000_identity(self):
        self.assertEqual(self.w.client_usb_target(),(self.target,self.node,2))
    def test_rejects_sdr_hub_wrong_mac_and_bad_bus(self):
        cases=(('idVendor','0bda'),('idProduct','2838'),('bDeviceClass','09'),('busnum','0'),('devnum','128'))
        for name,value in cases:
            path=self.target/name;old=path.read_text();path.write_text(value)
            with self.assertRaises(RuntimeError):self.w.client_usb_target()
            path.write_text(old)
        (self.network/'address').write_text(self.w.AP_MAC)
        with self.assertRaises(RuntimeError):self.w.client_usb_target()
    def test_duplicate_or_ap_identity_is_rejected(self):
        duplicate=self.net/'wlan2';duplicate.mkdir();(duplicate/'address').write_text(self.w.CLIENT_MAC);(duplicate/'device').symlink_to(self.target/'1-3:1.0',target_is_directory=True)
        with self.assertRaises(RuntimeError):self.w.client_usb_target()
        with patch.object(self.w,'CLIENT_MAC',self.w.AP_MAC):
            with self.assertRaises(RuntimeError):self.w.client_usb_target()
    def test_only_validated_character_device_receives_reset(self):
        import fcntl,os,stat
        good=SimpleNamespace(st_mode=stat.S_IFCHR,st_rdev=os.makedev(189,2))
        with patch.object(self.w.os,'geteuid',return_value=0),patch.object(self.w.os,'fstat',return_value=good),patch.object(fcntl,'ioctl') as ioctl:
            result=self.w.reset_client_usb()
        self.assertIn('Targeted Linksys',result['message']);self.assertEqual(ioctl.call_args.args[1:],(0x5514,0))
        for wrong in (SimpleNamespace(st_mode=stat.S_IFREG,st_rdev=good.st_rdev),SimpleNamespace(st_mode=stat.S_IFCHR,st_rdev=os.makedev(189,3))):
            with patch.object(self.w.os,'geteuid',return_value=0),patch.object(self.w.os,'fstat',return_value=wrong),patch.object(fcntl,'ioctl') as ioctl:
                with self.assertRaises(RuntimeError):self.w.reset_client_usb()
                ioctl.assert_not_called()
    def test_nonroot_and_node_symlink_are_rejected(self):
        import fcntl
        with patch.object(self.w.os,'geteuid',return_value=1000),patch.object(fcntl,'ioctl') as ioctl:
            with self.assertRaises(RuntimeError):self.w.reset_client_usb()
            ioctl.assert_not_called()
        self.node.unlink();self.node.symlink_to(self.network/'address')
        with patch.object(self.w.os,'geteuid',return_value=0),patch.object(fcntl,'ioctl') as ioctl:
            with self.assertRaises(OSError):self.w.reset_client_usb()
            ioctl.assert_not_called()
    def test_helper_is_a_fixed_argument_bounded_child(self):
        with patch.object(self.w.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"message":"reset"}')) as run:
            self.assertEqual(self.w.request_client_usb_reset(),{'message':'reset'})
        self.assertEqual(run.call_args.args[0],['/usr/bin/python3',str(Path(self.w.__file__).resolve()),'--reset-client-usb'])
        self.assertEqual(run.call_args.kwargs['timeout'],12)

    def test_pending_management_restore_follows_mac_after_interface_renumbering(self):
        self.w.CLIENT_MANAGEMENT_RESTORE=True
        with patch.object(self.w,'device',return_value=None),patch.object(self.w,'nm') as nm:
            self.assertFalse(self.w.restore_client_management());nm.assert_not_called()
        self.assertTrue(self.w.CLIENT_MANAGEMENT_RESTORE)
        with patch.object(self.w,'device',return_value='wlan7'),patch.object(self.w,'nm') as nm:
            self.assertTrue(self.w.restore_client_management())
        nm.assert_called_once_with('device','set','wlan7','managed','yes')
        self.assertFalse(self.w.CLIENT_MANAGEMENT_RESTORE)
