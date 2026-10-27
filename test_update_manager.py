"""Fault-injection checks for app updates; no real services, network or device changes."""
import hashlib,json,sqlite3,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
import update_manager as u

class UpdateTests(unittest.TestCase):
    def setUp(self):
        auth=patch.object(u,'authenticate',side_effect=lambda value,record=False:u.validate(value));auth.start();self.addCleanup(auth.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name)
        for name,path in {'APP':root/'app','STATE':root/'state','BACKUPS':root/'backups','DATA':root/'data.sqlite3','HARDWARE':root/'hardware.json'}.items():
            p=patch.object(u,name,path);p.start();self.addCleanup(p.stop)
        u.APP.mkdir();u.STATE.mkdir();u.BACKUPS.mkdir()
        for name in u.FILES:(u.APP/name).write_text("VERSION='0.3.10'\n" if name=='mobile.py' else '# original\n')
        with sqlite3.connect(u.DATA) as db:db.execute('CREATE TABLE receptions(id INTEGER)');db.execute('INSERT INTO receptions VALUES(42)')
        self.source={name:("VERSION='0.3.11'\n" if name=='mobile.py' else '# updated\n').encode() for name in u.FILES}
        self.value=dict(schema=2,compatibility='app-only-v1',version='0.3.11',commit='a'*40,files={n:hashlib.sha256(b).hexdigest() for n,b in self.source.items()})
    def folder(self):
        path=u.STATE/'fixture';path.mkdir()
        for name,data in self.source.items():(path/name).write_bytes(data)
        return path
    def test_manifest_rejects_paths_missing_files_and_bad_hashes(self):
        for mutate in (lambda d:d['files'].update({'../oops.py':'b'*64}),lambda d:d['files'].pop('mobile.py'),lambda d:d.update(commit='../../main'),lambda d:d.update(version='latest'),lambda d:d.update(compatibility='system-upgrade')):
            data=json.loads(json.dumps(self.value));mutate(data)
            with self.assertRaises(RuntimeError):u.validate(data)
    def test_download_hash_failure_keeps_application_unchanged(self):
        with patch.object(u,'download',return_value=b'# wrong'),patch.object(u,'services') as services:
            with self.assertRaises(RuntimeError):u.stage(self.value)
        services.assert_not_called();self.assertEqual(u.app_version(),'0.3.10')
    def test_stage_checks_source_version_and_uses_commit_urls(self):
        urls=[]
        def download(url,limit):urls.append(url);return self.source[url.rsplit('/',1)[1]]
        with patch.object(u,'download',side_effect=download):folder=u.stage(self.value)
        self.assertTrue(all('/'+'a'*40+'/' in url for url in urls));self.assertEqual(u.app_version(folder/'mobile.py'),'0.3.11')
        self.assertTrue((folder/'manifest.json').is_file())
    def test_staged_tampering_refused_before_reception_stops(self):
        folder=self.folder();(folder/'mobile.py').write_text("VERSION='9.9.9'")
        with patch.object(u,'services') as services:
            with self.assertRaises(RuntimeError):u.apply(self.value,folder)
        services.assert_not_called()
    def test_success_preserves_data_and_settings_and_backs_up_database(self):
        u.put(u.STATE/'settings.json',dict(u.DEFAULT,scheduled=False));u.HARDWARE.write_text('private-local-settings')
        with patch.object(u,'services') as services,patch.object(u,'health') as health:u.apply(self.value,self.folder())
        self.assertEqual(u.app_version(),'0.3.11');self.assertFalse(u.settings()['scheduled']);self.assertEqual(u.HARDWARE.read_text(),'private-local-settings')
        self.assertFalse((u.STATE/'pending.json').exists());health.assert_called_once_with('0.3.11')
        with sqlite3.connect(u.DATA) as db:self.assertEqual(db.execute('SELECT id FROM receptions').fetchone()[0],42)
        backup=next(u.BACKUPS.iterdir())/'receptions.sqlite3'
        with sqlite3.connect(backup) as db:self.assertEqual(db.execute('SELECT id FROM receptions').fetchone()[0],42)
    def test_health_failure_rolls_back_all_source_and_keeps_history(self):
        original={n:(u.APP/n).read_bytes() for n in u.FILES}
        with patch.object(u,'services'),patch.object(u,'health',side_effect=RuntimeError('bad API')):
            with self.assertRaises(RuntimeError):u.apply(self.value,self.folder())
        self.assertEqual({n:(u.APP/n).read_bytes() for n in u.FILES},original)
        self.assertFalse((u.STATE/'pending.json').exists());self.assertEqual(u.status()['last_result'],'rolled_back')
        with sqlite3.connect(u.DATA) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM receptions').fetchone()[0],1)
    def test_interrupted_transaction_recovers_on_next_start(self):
        backup=u.BACKUPS/'interrupted';backup.mkdir()
        for name in u.FILES:(backup/name).write_bytes((u.APP/name).read_bytes())
        u.put(u.STATE/'pending.json',dict(backup=str(backup),absent=[]))
        (u.APP/'mobile.py').write_text("VERSION='0.3.11'\n")
        u.recover();self.assertEqual(u.app_version(),'0.3.10');self.assertFalse((u.STATE/'pending.json').exists())
    def test_bad_database_prevents_stopping_receiver(self):
        u.DATA.write_bytes(b'not a database')
        with patch.object(u,'services') as services:
            with self.assertRaises(sqlite3.DatabaseError):u.apply(self.value,self.folder())
        services.assert_not_called()
    def test_automatic_toggle_is_boolean_and_unknown_parameters_rejected(self):
        with patch.object(u.os,'geteuid',return_value=0):
            self.assertFalse(u.broker_action(dict(action='update',operation='set_auto',enabled=False))['automatic'])
            for value in (dict(action='update',operation='set_auto',enabled='false'),dict(action='update',operation='install',url='https://invalid.example'),dict(action='update',operation='shell')):
                with self.assertRaises(RuntimeError):u.broker_action(value)
    def test_nonroot_control_rejected(self):
        with patch.object(u.os,'geteuid',return_value=999):
            with self.assertRaises(RuntimeError):u.broker_action(dict(action='update',operation='install'))
    def test_control_queues_fixed_units_without_waiting_for_restart(self):
        u.put(u.STATE/'settings.json',u.DEFAULT)
        with patch.object(u.os,'geteuid',return_value=0),patch.object(u,'command') as command:
            u.broker_action(dict(action='update',operation='install'))
        command.assert_called_once_with(['systemctl','start','--no-block','pisonde-update-install.service'])
    def run_case(self,action,enabled,hour):
        u.put(u.STATE/'settings.json',dict(u.DEFAULT,scheduled=enabled))
        with patch.object(u,'command'),patch.object(u,'catalog',return_value=self.value),patch.object(u,'stage',return_value=self.folder()),patch.object(u,'apply') as apply,patch.object(u,'datetime') as clock:
            clock.now.return_value=datetime(2026,10,27,hour);u.run(action)
        return apply
    def test_automatic_install_only_in_window(self):self.assertTrue(self.run_case('run',True,4).called)
    def test_outside_window_downloads_but_does_not_install(self):self.assertFalse(self.run_case('run',True,12).called)
    def test_disabled_automatic_never_installs_on_timer(self):self.assertFalse(self.run_case('run',False,4).called)
    def test_manual_check_does_not_install(self):self.assertFalse(self.run_case('check',True,4).called)
    def test_manual_install_works_outside_window(self):self.assertTrue(self.run_case('install',False,12).called)
    def test_never_downgrades(self):
        self.value['version']='0.3.9'
        with patch.object(u,'command'),patch.object(u,'catalog',return_value=self.value),patch.object(u,'stage') as stage:u.run('install')
        stage.assert_not_called()
    def test_network_failure_keeps_old_receiver(self):
        with patch.object(u,'command'),patch.object(u,'catalog',side_effect=OSError('offline')),patch.object(u,'services') as services:
            with self.assertRaises(OSError):u.run('run')
        services.assert_not_called();self.assertEqual(u.app_version(),'0.3.10');self.assertIn('offline',u.status()['last_error'])
    def test_legacy_hardware_migration_does_not_read_or_change_wifi_secrets(self):
        (u.APP/'wifi_control.py').write_text("ONBOARD='02:00:00:00:00:01'\nUSB='02:00:00:00:00:02'\nAP='11111111-2222-4333-8444-555555555555'\n")
        (u.APP/'mobile.py').write_text("VERSION='0.3.10'\nDEVICE='DEMO_SDR'\n")
        u.legacy_hardware();data=json.loads(u.HARDWARE.read_text());self.assertEqual(data['sdr_serial'],'DEMO_SDR');self.assertNotIn('password',data)
        u.HARDWARE.write_text('preserve');u.legacy_hardware();self.assertEqual(u.HARDWARE.read_text(),'preserve')

    def test_install_support_preserves_disabled_setting_and_orders_recovery(self):
        u.put(u.STATE/'settings.json',dict(u.DEFAULT,scheduled=False))
        units=Path(self.temp.name)/'units'
        with patch.object(u.os,'geteuid',return_value=0),patch.object(u,'command') as command:u.install_support(units)
        self.assertFalse(u.settings()['scheduled'])
        calls=[c.args[0] for c in command.call_args_list]
        self.assertLess(calls.index(['systemctl','start','pisonde-update-recover.service']),calls.index(['systemctl','restart','pisonde-update.timer']))
        self.assertIn('OnUnitActiveSec=6h',(units/'pisonde-update.timer').read_text());self.assertIn('OnCalendar=*-*-* *:15:00',(units/'pisonde-update.timer').read_text())
        for name in u.UNITS:self.assertIn('Requires=pisonde-update-recover.service',(units/(name+'.d')/'updates.conf').read_text())
    def test_recovery_of_running_app_stops_before_restore(self):
        u.put(u.STATE/'pending.json',{})
        events=[]
        with patch.object(u,'services',side_effect=lambda action:events.append(action)),patch.object(u,'recover',side_effect=lambda:events.append('restore')):u.recover_running()
        self.assertEqual(events,['stop','restore','start'])

    def test_bootstrap_preserves_legacy_upload_identity_when_not_yet_saved(self):
        (u.APP/'field_features.py').write_text("DEFAULT=dict(enabled=True,callsign='N0TEST-3',aprs_port=14580)\n")
        from types import SimpleNamespace
        with patch.object(u.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=999,pw_gid=999)),patch.object(u.os,'chown'):
            u.legacy_upload_settings()
        path=u.DATA.parent/'uploads.json';settings=json.loads(path.read_text())
        self.assertTrue(settings['enabled']);self.assertEqual(settings['callsign'],'N0TEST-3');self.assertEqual(path.stat().st_mode&0o777,0o600)
        path.write_text('preserve-existing');u.legacy_upload_settings();self.assertEqual(path.read_text(),'preserve-existing')

    def test_bootstrap_uses_supported_ownership_api_and_completes_install(self):
        from types import SimpleNamespace
        folder=self.folder()
        with patch.object(u,'UPDATER_VERSION','0.3.11'),patch.object(u.os,'geteuid',return_value=0),patch.object(u.os,'chown') as chown,patch.object(u.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=999,pw_gid=999)),patch.object(u,'catalog',return_value=self.value),patch.object(u,'legacy_hardware'),patch.object(u,'legacy_upload_settings'),patch.object(u,'stage',return_value=folder),patch.object(u,'install_support') as support,patch.object(u,'services'),patch.object(u,'health'),patch.object(u,'command') as command:
            u.bootstrap()
        chown.assert_any_call(u.APP,0,0)
        self.assertEqual((next(u.BACKUPS.iterdir())/'update_manager.py').read_bytes(),b'# original\n')
        support.assert_called_once_with(start_timer=False)
        self.assertEqual(u.app_version(),'0.3.11')
        self.assertFalse((u.STATE/'pending.json').exists())
        command.assert_called_with(['systemctl','start','pisonde-update.timer'])
    def test_catalog_uses_api_content_and_preserves_manifest_identity(self):
        import base64
        entry=dict(encoding='base64',path='updates/signed-stable.json',sha='b'*40,content=base64.b64encode(json.dumps(self.value).encode()).decode())
        with patch.object(u,'download',return_value=json.dumps(entry).encode()) as download:value=u.catalog()
        self.assertEqual(value['commit'],self.value['commit']);self.assertNotIn('channel_blob_sha',value)
        self.assertTrue(download.call_args.args[0].startswith(u.API+'contents/updates/signed-stable.json?ref=main&check='))
    def test_queued_check_completes_with_same_operation_id(self):
        u.put(u.STATE/'settings.json',u.DEFAULT)
        with patch.object(u.os,'geteuid',return_value=0),patch.object(u,'command'):
            queued=u.broker_action(dict(action='update',operation='check'))
            with patch.object(u,'catalog',return_value=self.value),patch.object(u,'stage',return_value=self.folder()):u.run('check')
        status=u.status();self.assertEqual(status['operation_id'],queued['operation_id']);self.assertEqual(status['outcome'],'available');self.assertTrue(status['completed_at'])
    def test_check_failure_is_completed_not_permanently_queued(self):
        u.put(u.STATE/'settings.json',u.DEFAULT)
        with patch.object(u.os,'geteuid',return_value=0),patch.object(u,'command'):
            queued=u.broker_action(dict(action='update',operation='check'))
            with patch.object(u,'catalog',side_effect=OSError('no connection')):
                with self.assertRaises(OSError):u.run('check')
        status=u.status();self.assertEqual(status['operation_id'],queued['operation_id']);self.assertEqual(status['outcome'],'failed');self.assertTrue(status['completed_at']);self.assertIn('no connection',status['last_error'])
    def test_duplicate_queued_operations_are_rejected(self):
        u.put(u.STATE/'settings.json',u.DEFAULT)
        with patch.object(u.os,'geteuid',return_value=0),patch.object(u,'command') as command:
            u.broker_action(dict(action='update',operation='check'))
            with self.assertRaises(RuntimeError):u.broker_action(dict(action='update',operation='check'))
        self.assertEqual(command.call_count,1)
    def test_worker_start_timeout_is_visible(self):
        from datetime import timedelta
        u.update_status(phase='Queued',operation='check',queued_at=(datetime.now(timezone.utc)-timedelta(seconds=100)).isoformat(),started_at=None)
        self.assertEqual(u.status()['outcome'],'timeout')
    def test_bootstrap_rejects_stale_channel_before_installation(self):
        with patch.object(u.os,'geteuid',return_value=0),patch.object(u.pwd,'getpwnam'),patch.object(u,'catalog',return_value=self.value),patch.object(u,'legacy_hardware') as migrate:
            with self.assertRaises(RuntimeError):u.bootstrap()
        migrate.assert_not_called()

    def test_old_disabled_settings_do_not_enable_new_options(self):
        u.put(u.STATE/'settings.json',dict(automatic=False,window_start=3,window_end=5))
        self.assertEqual(u.settings(),dict(u.DEFAULT,scheduled=False))
        self.assertFalse(u.status()['automatic'])
    def test_policy_accepts_all_windows_and_rejects_untrusted_fields(self):
        with patch.object(u.os,'geteuid',return_value=0),patch.object(u,'command'):
            for start,end in u.WINDOWS:
                cfg=dict(u.DEFAULT,window_start=start,window_end=end,immediate=True,reboot_if_missed=True)
                result=u.broker_action(dict(action='update',operation='set_policy',settings=cfg))
                self.assertEqual(result['window'],f'{start:02d}00–{end:02d}00 local time')
            for bad in (dict(u.DEFAULT,immediate='yes'),dict(u.DEFAULT,window_start=True),dict(u.DEFAULT,window_start=4),dict(u.DEFAULT,shell='anything')):
                with self.assertRaises(RuntimeError):u.broker_action(dict(action='update',operation='set_policy',settings=bad))
    def test_every_option_combination(self):
        from itertools import product
        local=datetime(2026,10,28,12,tzinfo=timezone.utc)
        policy=dict(available_since='2026-10-27T02:00:00+00:00',boot_id='old')
        for scheduled,reboot,immediate in product((False,True),repeat=3):
            cfg=dict(u.DEFAULT,scheduled=scheduled,reboot_if_missed=reboot,immediate=immediate)
            outside=u.install_reason(cfg,'run',policy,local,'new',local)
            inside=u.install_reason(cfg,'run',policy,local.replace(hour=4),'new',local.replace(hour=4))
            sameboot=u.install_reason(cfg,'run',dict(policy,boot_id='new'),local,'new',local)
            self.assertEqual(bool(outside),reboot or immediate,(cfg,outside))
            self.assertEqual(bool(inside),scheduled or reboot or immediate,(cfg,inside))
            self.assertEqual(bool(sameboot),immediate,(cfg,sameboot))
    def test_reboot_does_not_install_update_first_seen_after_missed_window(self):
        local=datetime(2026,10,28,12,tzinfo=timezone.utc)
        cfg=dict(u.DEFAULT,reboot_if_missed=True)
        for seen in ('2026-10-28T05:00:00+00:00','2026-10-28T10:00:00+00:00'):
            self.assertFalse(u.install_reason(cfg,'run',dict(available_since=seen,boot_id='old'),local,'new',local))
    def test_window_boundaries_and_midnight(self):
        cfg=dict(u.DEFAULT,window_start=23,window_end=1)
        for hour,expected in ((22,False),(23,True),(0,True),(1,False)):
            self.assertEqual(u.in_window(cfg,datetime(2026,10,28,hour)),expected)
        local=datetime(2026,10,28,0,30,tzinfo=timezone.utc)
        self.assertEqual(u.last_window_end(cfg,local),datetime(2026,10,27,1,tzinfo=timezone.utc))
    def test_manual_check_installs_only_when_immediate_enabled(self):
        local=datetime(2026,10,28,4,tzinfo=timezone.utc)
        policy=dict(available_since='2026-10-27T02:00:00+00:00',boot_id='old')
        self.assertFalse(u.install_reason(dict(u.DEFAULT,reboot_if_missed=True),'check',policy,local,'new',local))
        self.assertTrue(u.install_reason(dict(u.DEFAULT,immediate=True),'check',policy,local,'new',local))
    def test_policy_saved_by_broker_is_used_by_worker(self):
        local=datetime(2026,10,28,12,tzinfo=timezone.utc)
        with patch.object(u.os,'geteuid',return_value=0),patch.object(u,'command') as command:
            u.broker_action(dict(action='update',operation='set_policy',settings=dict(u.DEFAULT,scheduled=False,immediate=True)))
        with patch.object(u,'command'),patch.object(u,'catalog',return_value=self.value),patch.object(u,'stage',return_value=self.folder()),patch.object(u,'apply') as apply,patch.object(u,'datetime') as clock:
            clock.now.return_value=local;clock.fromisoformat.side_effect=datetime.fromisoformat;u.run('check')
        apply.assert_called_once()
        command.assert_called_once_with(['systemctl','start','--no-block','pisonde-update-run.service'])
    def test_missed_window_retries_only_on_new_boot(self):
        local=datetime(2026,10,28,12,tzinfo=timezone.utc)
        u.put(u.STATE/'settings.json',dict(u.DEFAULT,scheduled=False,reboot_if_missed=True))
        u.put(u.STATE/'policy.json',dict(commit=self.value['commit'],available_since='2026-10-27T02:00:00+00:00',boot_id='old'))
        with patch.object(u,'command'),patch.object(u,'catalog',return_value=self.value),patch.object(u,'stage',return_value=self.folder()),patch.object(u,'apply') as apply,patch.object(u,'boot_id',return_value='new'),patch.object(u,'boot_time',return_value=local),patch.object(u,'datetime') as clock:
            clock.now.return_value=local;clock.fromisoformat.side_effect=datetime.fromisoformat;u.run('run');self.assertEqual(apply.call_count,1)
            u.run('run');self.assertEqual(apply.call_count,1)
        self.assertEqual(u.read(u.STATE/'policy.json')['boot_id'],'new')
    def test_network_failure_does_not_consume_reboot_retry(self):
        u.put(u.STATE/'policy.json',dict(boot_id='old'))
        with patch.object(u,'command'),patch.object(u,'catalog',side_effect=OSError('offline')):
            with self.assertRaises(OSError):u.run('run')
        self.assertEqual(u.read(u.STATE/'policy.json')['boot_id'],'old')
    def test_hourly_policy_evaluation_reuses_recent_download(self):
        u.put(u.STATE/'settings.json',dict(u.DEFAULT,scheduled=False))
        self.folder().rename(u.STATE/self.value['commit'])
        (u.STATE/'staged').mkdir();(u.STATE/self.value['commit']).rename(u.STATE/'staged'/self.value['commit'])
        u.put(u.STATE/'available.json',self.value);u.update_status(last_check=u.now())
        with patch.object(u,'command'),patch.object(u,'catalog') as catalog,patch.object(u,'stage') as stage:u.run('run')
        catalog.assert_not_called();stage.assert_not_called()
    def test_support_migrates_old_timer_once(self):
        u.put(u.STATE/'settings.json',dict(automatic=False,window_start=3,window_end=5))
        with patch.object(u,'install_support') as support:
            u.ensure_support();support.assert_called_once()
            u.put(u.STATE/'support.json',dict(version=u.UPDATER_VERSION));u.ensure_support();self.assertEqual(support.call_count,1)

    def test_failed_boot_install_retries_until_success(self):
        local=datetime(2026,10,28,12,tzinfo=timezone.utc)
        u.put(u.STATE/'settings.json',dict(u.DEFAULT,scheduled=False,reboot_if_missed=True))
        u.put(u.STATE/'policy.json',dict(commit=self.value['commit'],available_since='2026-10-27T02:00:00+00:00',boot_id='old'))
        folder=self.folder()
        with patch.object(u,'command'),patch.object(u,'catalog',return_value=self.value),patch.object(u,'stage',return_value=folder),patch.object(u,'apply',side_effect=[RuntimeError('not ready'),None]) as apply,patch.object(u,'boot_id',return_value='new'),patch.object(u,'boot_time',return_value=local),patch.object(u,'datetime') as clock:
            clock.now.return_value=local;clock.fromisoformat.side_effect=datetime.fromisoformat
            with self.assertRaises(RuntimeError):u.run('run')
            self.assertEqual(u.read(u.STATE/'policy.json')['boot_id'],'old')
            u.run('run');self.assertEqual(apply.call_count,2)
        self.assertEqual(u.read(u.STATE/'policy.json')['boot_id'],'new')
    def test_maintenance_window_uses_local_offset(self):
        from datetime import timedelta
        local=datetime(2026,10,28,4,tzinfo=timezone(timedelta(hours=-4)))
        self.assertTrue(u.in_window(u.DEFAULT,local))
        self.assertFalse(u.in_window(u.DEFAULT,local.astimezone(timezone.utc)))
