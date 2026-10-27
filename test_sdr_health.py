"""Offline fault-injection checks. Never access or reset real USB hardware."""
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sdr_health as h

class HealthTests(unittest.TestCase):
    def monitor(self):
        clock=[0.];count=[100]
        monitor=h.FlowMonitor(123,clock=lambda:clock[0],counter=lambda pid:count[0])
        return monitor,clock,count

    def test_no_samples_at_startup(self):
        monitor,clock,count=self.monitor()
        count[0]+=1500  # tuner messages must not qualify as samples
        clock[0]=5.9;self.assertFalse(monitor.check())
        clock[0]=6
        with self.assertRaises(h.RadioFailure):monitor.check()

    def test_radio_silence_with_samples_is_healthy(self):
        monitor,clock,count=self.monitor()
        for t in range(1,101):
            clock[0]=t;count[0]+=48000
            self.assertTrue(monitor.check())

    def test_stream_stops_after_initial_success(self):
        monitor,clock,count=self.monitor()
        clock[0]=1;count[0]+=48000;self.assertTrue(monitor.check())
        clock[0]=6.9;self.assertTrue(monitor.check())
        clock[0]=7
        with self.assertRaises(h.RadioFailure):monitor.check()

    def test_small_chunks_accumulate(self):
        monitor,clock,count=self.monitor()
        for t in range(1,5):
            clock[0]=t;count[0]+=4096
            self.assertEqual(monitor.check(),t==4)

    def test_counter_disappears_or_rewinds(self):
        monitor,clock,count=self.monitor();count[0]=0
        with self.assertRaises(h.RadioFailure):monitor.check()

    def test_recovery_escalation_and_cooldown(self):
        clock=[0.];r=h.Recovery(clock=lambda:clock[0])
        self.assertEqual(r.fault('one'),'restart')
        self.assertEqual(r.fault('two'),'reset')
        clock[0]=119;self.assertEqual(r.fault('three'),'wait')
        clock[0]=120;self.assertEqual(r.fault('four'),'reset')
        r.healthy();self.assertEqual(r.fault('new'),'restart')

    def make_device(self,root,name,serial='00000001',vendor='0bda',product='2838'):
        d=root/name;d.mkdir()
        for key,value in dict(idVendor=vendor,idProduct=product,serial=serial,busnum='1',devnum='7').items():
            (d/key).write_text(value)

    def test_reset_target_excludes_wifi_hub_and_other_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.make_device(root,'wifi',vendor='13b1',product='003e')
            self.make_device(root,'hub',vendor='2109',product='3431')
            self.make_device(root,'other-sdr',serial='OTHER')
            with self.assertRaises(RuntimeError):h.target_device(root)
            self.make_device(root,'expected')
            self.assertEqual(h.target_device(root),(1,7))
            self.make_device(root,'duplicate')
            with self.assertRaises(RuntimeError):h.target_device(root)

    def test_no_reset_while_receiver_active(self):
        with patch.object(h.subprocess,'run',return_value=SimpleNamespace(returncode=0)),patch.object(h.os,'open') as op:
            with self.assertRaises(RuntimeError):h.reset_device()
            op.assert_not_called()

    def test_reset_only_verified_character_node(self):
        info=SimpleNamespace(st_mode=stat.S_IFCHR,st_rdev=__import__('os').makedev(189,6))
        with patch.object(h.subprocess,'run',return_value=SimpleNamespace(returncode=1)), \
             patch.object(h,'target_device',return_value=(1,7)), \
             patch.object(h.os,'open',return_value=91) as op, \
             patch.object(h.os,'fstat',return_value=info),patch.object(h.os,'close'), \
             patch.object(h.fcntl,'ioctl') as ioctl:
            self.assertEqual(h.reset_device()['serial'],'00000001')
            self.assertEqual(op.call_args.args[0],'/dev/bus/usb/001/007')
            ioctl.assert_called_once_with(91,0x5514,0)
            info.st_rdev=__import__('os').makedev(189,7)
            with self.assertRaises(RuntimeError):h.reset_device()
            self.assertEqual(ioctl.call_count,1)

    def test_broker_persistent_reset_limits_and_bounded_child(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(h,'RESET_HISTORY',Path(tmp)/'history.json'), \
             patch.object(h.time,'monotonic',return_value=1000) as clock, \
             patch.object(h.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"serial":"00000001"}')) as run:
            self.assertEqual(h.broker_reset()['serial'],'00000001')
            self.assertEqual(run.call_args.kwargs['timeout'],12)
            with self.assertRaises(RuntimeError):h.broker_reset()
            clock.return_value=1120;h.broker_reset()
            clock.return_value=1240;h.broker_reset()
            clock.return_value=1360
            with self.assertRaises(RuntimeError):h.broker_reset()
            self.assertEqual(run.call_count,3)

    def test_pipeline_detects_actual_process_output_then_freeze(self):
        spec=importlib.util.spec_from_file_location('health_mobile',Path(__file__).with_name('mobile.py'))
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        cmd=[sys.executable,'-u','-c','import os,time;os.write(1,b"x"*20000+bytes([10]));time.sleep(20)']
        sink=[sys.executable,'-u','-c','import sys,time;data=sys.stdin.buffer.read(20001);print(len(data),flush=True);time.sleep(20)']
        with m.Pipeline([cmd,sink],monitor_samples=True) as p:
            p.flow.timeout=.6
            self.assertEqual(p.output.get(timeout=3),'20001')
            self.assertEqual(p.sample_bytes,20001)
            self.assertFalse(p.check_flow())
            time.sleep(.65)
            with self.assertRaises(h.RadioFailure):p.check_flow()
            child=p.processes[0]
        self.assertIsNotNone(child.poll())

    def test_exact_repeat_detected_despite_continuing_byte_flow(self):
        import random
        clock=[0.]
        monitor=h.FreshnessMonitor(clock=lambda:clock[0])
        period=random.Random(3).randbytes(1880)
        stream=period*200
        offset=0
        for second in range(7):
            clock[0]=second
            block=stream[offset:offset+40001];offset+=40001
            # Uneven read boundaries must not hide a continuous periodic stream.
            for start in range(0,len(block),997):
                monitor.feed(block[start:start+997])
        with self.assertRaises(h.RadioFailure):monitor.check()
        self.assertLessEqual(len(monitor.buffer),32768)

    def test_noise_with_no_packets_is_fresh(self):
        import random
        clock=[0.];monitor=h.FreshnessMonitor(clock=lambda:clock[0])
        rng=random.Random(5)
        for second in range(15):
            clock[0]=second;monitor.feed(rng.randbytes(48000))
            self.assertEqual(monitor.check(),(True,None))

    def test_brief_periodic_signal_clears_without_fault(self):
        import random
        clock=[0.];monitor=h.FreshnessMonitor(clock=lambda:clock[0])
        monitor.feed(b'abc'*12000)
        clock[0]=5;monitor.feed(b'abc'*12000)
        self.assertFalse(monitor.check()[0])
        clock[0]=5.5;monitor.feed(random.Random(1).randbytes(40000))
        self.assertEqual(monitor.check(),(True,None))

    def test_repeats_cannot_clear_recovery_failures(self):
        spec=importlib.util.spec_from_file_location('repeat_mobile',Path(__file__).with_name('mobile.py'))
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        clock=[0.];count=[0]
        p=m.Pipeline([[sys.executable]],monitor_samples=True)
        p.processes=[SimpleNamespace(poll=lambda:None)]
        p.flow=h.FlowMonitor(123,counter=lambda _:count[0],clock=lambda:clock[0])
        p.freshness=h.FreshnessMonitor(clock=lambda:clock[0])
        with patch.object(m.radio_recovery,'healthy') as healthy:
            for second in range(6):
                clock[0]=second;count[0]+=48000
                p.freshness.feed(b'ab'*24000)
                self.assertFalse(p.check_flow())
            clock[0]=6;count[0]+=48000;p.freshness.feed(b'ab'*24000)
            with self.assertRaises(h.RadioFailure):p.check_flow()
            healthy.assert_not_called()

    def test_stop_profile_cancels_recovery_before_reset(self):
        spec=importlib.util.spec_from_file_location('health_mobile',Path(__file__).with_name('mobile.py'))
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        m.shutdown_event.set()
        with patch.object(m,'request_reset') as reset:
            m.recover_radio(h.RadioFailure('stalled'),0)
            reset.assert_not_called()

    def test_buffered_startup_burst_does_not_clear_recovery_failures(self):
        spec=importlib.util.spec_from_file_location('health_mobile',Path(__file__).with_name('mobile.py'))
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        clock=[0.];count=[0]
        p=m.Pipeline([[sys.executable]])
        p.processes=[SimpleNamespace(poll=lambda:None)]
        p.flow=h.FlowMonitor(123,counter=lambda _:count[0],clock=lambda:clock[0])
        p.freshness.feed(__import__('random').Random(4).randbytes(32768))
        with patch.object(m.time,'monotonic',side_effect=lambda:clock[0]),patch.object(m.radio_recovery,'healthy') as healthy:
            count[0]=20000;clock[0]=1;self.assertTrue(p.check_flow())
            clock[0]=3.1;self.assertTrue(p.check_flow())
            healthy.assert_not_called()
            count[0]+=20000;clock[0]=3.2;self.assertTrue(p.check_flow())
            healthy.assert_called_once()

    def test_sample_relay_preserves_every_byte(self):
        import hashlib
        spec=importlib.util.spec_from_file_location('health_mobile',Path(__file__).with_name('mobile.py'))
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        expected=bytes(range(256))*4096
        source=[sys.executable,'-u','-c','import os;data=bytes(range(256))*4096;os.write(1,data)']
        sink=[sys.executable,'-u','-c','import sys,hashlib;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())']
        with m.Pipeline([source,sink],monitor_samples=True) as p:
            self.assertEqual(p.output.get(timeout=5),hashlib.sha256(expected).hexdigest())
            self.assertEqual(p.sample_bytes,len(expected))

    def test_stderr_noise_never_counts_as_samples(self):
        spec=importlib.util.spec_from_file_location('health_mobile',Path(__file__).with_name('mobile.py'))
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        source=[sys.executable,'-u','-c','import os,time;os.write(2,b"tuner initialized"+bytes([10]));time.sleep(20)']
        sink=[sys.executable,'-u','-c','import sys;sys.stdin.buffer.read()']
        with m.Pipeline([source,sink],monitor_samples=True) as p:
            self.assertFalse(p.check_flow())
            self.assertEqual(p.sample_bytes,0)
            p.flow.last_progress-=7
            with self.assertRaises(h.RadioFailure):p.check_flow()

    def test_recovery_runs_after_children_close_and_keeps_manual_config(self):
        spec=importlib.util.spec_from_file_location('health_mobile',Path(__file__).with_name('mobile.py'))
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        m.config=dict(m.DEFAULT,profile='MANUAL',manual_mode='APRS',manual_mhz=434.2)
        original=dict(m.config);children=[];recovered=[]
        source=[sys.executable,'-u','-c','import time;time.sleep(20)']
        sink=[sys.executable,'-u','-c','import sys;sys.stdin.buffer.read()']
        pipeline=m.Pipeline
        def make_pipeline(commands):
            p=pipeline(commands,monitor_samples=True)
            children.append(p)
            return p
        flow=m.FlowMonitor
        def fast_monitor(*args,**kwargs):return flow(*args,timeout=.1,**kwargs)
        def recover(exc,rev):
            recovered.append(str(exc))
            self.assertTrue(all(child.poll() is not None for child in children[0].processes))
            m.shutdown_event.set()
        with patch.object(m,'commands_for',return_value=[source,sink]), \
             patch.object(m,'Pipeline',side_effect=make_pipeline), \
             patch.object(m,'FlowMonitor',side_effect=fast_monitor),patch.object(m,'recover_radio',side_effect=recover):
            m.scanner()
        self.assertEqual(m.config,original)
        self.assertEqual(len(recovered),1)

    def test_reset_action_rejects_device_parameters(self):
        spec=importlib.util.spec_from_file_location('health_broker',Path(__file__).with_name('wifi_control.py'))
        w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
        with self.assertRaises(RuntimeError):w.handle({'action':'sdr_reset','device':'/dev/sda'})

    def test_field_only_closeout_never_induces_stalls(self):
        from unittest.mock import mock_open
        spec=importlib.util.spec_from_file_location('field_verifier',Path(__file__).with_name('verify_recovery.py'))
        v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
        baseline={'version':'0.3.10','config':{'profile':'STOP'}}
        with patch.object(v.sys,'argv',['verify_recovery.py','--field-only']), \
             patch.object(v.os,'geteuid',return_value=0),patch.object(v.socket,'gethostname',return_value='example-pi'), \
             patch.object(v,'open',mock_open(),create=True),patch.object(v.fcntl,'flock'), \
             patch.object(v,'state',return_value=baseline),patch.object(v,'service_pid',return_value=123), \
             patch.object(v,'verify_recovery') as recover,patch.object(v,'pause') as pause, \
             patch.object(v,'verify_field') as verify:
            self.assertEqual(v.main(),0)
            recover.assert_not_called();pause.assert_not_called();verify.assert_called_once_with(baseline)

if __name__=='__main__':unittest.main()
