"""Public packaging checks; never touches real adapters, services, or endpoints."""
import importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import hardware,configure

class PublicSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.trust_patch=patch('update_manager.trusted_key');self.trust_patch.start();self.addCleanup(self.trust_patch.stop)
        self.update_patch=patch('update_manager.install_support');self.update_patch.start();self.addCleanup(self.update_patch.stop)
        self.data=dict(onboard_mac='02:00:00:00:00:01',client_mac='02:00:00:00:00:02',ap_uuid='11111111-2222-4333-8444-555555555555',sdr_serial='DEMO_SDR')
    def load(self):
        path=self.root/'hardware.json';path.write_text(json.dumps(self.data))
        with patch.dict(os.environ,PISONDE_HARDWARE=str(path)):return hardware.load_hardware()
    def test_public_hardware_is_local_configuration(self):self.assertEqual(self.load(),self.data)
    def test_duplicate_adapters_rejected(self):
        self.data['client_mac']=self.data['onboard_mac']
        with self.assertRaises(RuntimeError):self.load()
    def test_multicast_mac_rejected(self):
        self.data['client_mac']='01:00:00:00:00:02'
        with self.assertRaises(RuntimeError):self.load()
    def test_serial_cannot_inject_arguments_or_paths(self):
        for value in ('../../dev/bus/usb','test\nserial',''):
            self.data['sdr_serial']=value
            with self.assertRaises(RuntimeError):self.load()
    def state(self):
        state=dict(hardware=self.data,profiles=[dict(uuid='client-id',interface='wlan0',mac='',cloned='')],active=['client-id'],ap_interface='wlan0',client_interface='wlan1')
        path=self.root/'state.json';path.write_text(json.dumps(state));return path
    def test_ap_activation_keeps_local_service_when_uplink_absent(self):
        calls=[]
        def nm(*args):
            calls.append(args)
            if args[:3]==('connection','up','client-id'):raise RuntimeError('offline')
            return ''
        with patch.object(configure,'STATE',self.state()),patch.object(configure,'nm',side_effect=nm),patch.object(configure,'run') as run:
            configure.activate()
        self.assertIn(('connection','up',self.data['ap_uuid'],'ifname','wlan0'),calls)
        self.assertIn(('connection','up','client-id','ifname','wlan1'),calls)
        self.assertEqual(run.call_args.args[0][:3],['systemctl','enable','--now'])
    def test_failed_ap_activation_rolls_back_before_enabling_services(self):
        path=self.root/'hardware.json';path.write_text('{}')
        def nm(*args):
            if args[:3]==('connection','up',self.data['ap_uuid']):raise RuntimeError('activation failed')
            return ''
        with patch.object(configure,'STATE',self.state()),patch.object(configure,'CONFIG',path),patch.object(configure,'nm',side_effect=nm) as mock,patch.object(configure,'run') as run:
            with self.assertRaises(RuntimeError):configure.activate()
        run.assert_not_called();self.assertFalse(path.exists())
        self.assertIn(('connection','up','client-id'),[c.args for c in mock.call_args_list])
    def test_uploads_require_explicit_identity_and_enable(self):
        import field_features as f
        self.assertFalse(f.DEFAULT['enabled']);self.assertEqual(f.DEFAULT['callsign'],'N0CALL')
        data=dict(f.DEFAULT,enabled=True)
        with self.assertRaises(ValueError):f.validate_settings(data)
        data['callsign']='N0TEST-3';f.validate_settings(data)

    def test_first_setup_creates_local_identities_without_persisting_password(self):
        config=self.root/'hardware.json';state=self.root/'setup.json'
        values={'connection.type':'802-11-wireless','802-11-wireless.mode':'infrastructure',
                'connection.interface-name':'wlan0','802-11-wireless.mac-address':'','802-11-wireless.cloned-mac-address':''}
        def nm(*args):
            if args[:2]==('-g','UUID'):return 'client-id'
            return ''
        def run(args,timeout=45):return '    * AP' if args[0]=='iw' else ''
        found={'wlan0':dict(mac=self.data['onboard_mac'],usb=False),'wlan1':dict(mac=self.data['client_mac'],usb=True)}
        with patch.object(configure,'CONFIG',config),patch.object(configure,'STATE',state),patch.object(configure.os,'geteuid',return_value=0),patch.object(configure,'adapters',return_value=found),patch.object(configure,'sdr_serial',return_value='DEMO_SDR'),patch.object(configure,'nm',side_effect=nm) as mock,patch.object(configure,'get',side_effect=lambda ident,key:values[key]),patch.object(configure,'run',side_effect=run) as commands,patch.object(configure.getpass,'getpass',return_value='example-password-only-for-unit-test'),patch.object(configure.sys,'argv',['configure.py']):
            configure.main()
        result=json.loads(config.read_text())
        self.assertEqual(result['onboard_mac'],self.data['onboard_mac']);self.assertEqual(result['client_mac'],self.data['client_mac'])
        self.assertNotEqual(result['ap_uuid'],self.data['ap_uuid'])
        self.assertNotIn('password',config.read_text());self.assertNotIn('example-password',state.read_text())
        self.assertEqual(state.stat().st_mode&0o777,0o600)
        self.assertTrue(any(c.args[0][0]=='systemd-run' for c in commands.call_args_list))
        self.assertTrue(any('wifi-sec.psk' in c.args for c in mock.call_args_list))
    def test_setup_refuses_existing_hardware_configuration(self):
        config=self.root/'hardware.json';config.write_text('{}')
        with patch.object(configure,'CONFIG',config),patch.object(configure.os,'geteuid',return_value=0),patch.object(configure.sys,'argv',['configure.py']),patch.object(configure,'adapters') as adapters:
            with self.assertRaises(RuntimeError):configure.main()
        adapters.assert_not_called()
    def test_adapter_role_selection_rejects_ambiguous_hardware(self):
        with self.assertRaises(RuntimeError):configure.choose({},True,'USB')

    def test_missing_enrollment_stops_setup_before_wifi_changes(self):
        config=self.root/'absent.json'
        with patch.object(configure,'CONFIG',config),patch.object(configure.os,'geteuid',return_value=0),patch.object(configure.sys,'argv',['configure.py']),patch('update_manager.trusted_key',side_effect=RuntimeError('not enrolled')),patch.object(configure,'adapters') as adapters:
            with self.assertRaises(RuntimeError):configure.main()
        adapters.assert_not_called()
    def test_missing_enrollment_stops_activation_before_wifi_changes(self):
        with patch('update_manager.trusted_key',side_effect=RuntimeError('not enrolled')),patch.object(configure,'nm') as nm:
            with self.assertRaises(RuntimeError):configure.activate()
        nm.assert_not_called()
