import importlib.util,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
HERE=Path(__file__).parent
class ForgetTests(unittest.TestCase):
 def setUp(self):
  spec=importlib.util.spec_from_file_location('forget_broker',HERE/'wifi_control.py');self.w=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.w)
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.w.FORGOTTEN=Path(self.tmp.name)/'forgotten.json'
  self.ap=dict(uuid=self.w.AP,ssid='pisondemobile',mac=self.w.AP_MAC,mode='ap')
  self.guest=dict(uuid='guest1',ssid='Example Guest',mac=self.w.CLIENT_MAC,mode='infrastructure')
  self.duplicate=dict(self.guest,uuid='guest2')
  self.dmz=dict(uuid='dmz',ssid='DMZ',mac=self.w.CLIENT_MAC,mode='infrastructure')
 def test_forget_deletes_duplicates_preserves_ap_and_dmz(self):
  with patch.object(self.w,'profiles',return_value=[self.ap,self.guest,self.duplicate,self.dmz]),patch.object(self.w,'nm') as nm:
   r=self.w.handle({'action':'forget','uuid':'guest1'})
  self.assertEqual(r['removed_profiles'],2)
  self.assertEqual([c.args for c in nm.call_args_list],[('connection','delete','guest1'),('connection','delete','guest2')])
  self.assertEqual(self.w.forgotten(),{'Example Guest'})
 def test_forget_without_linksys(self):
  with patch.object(self.w,'profiles',return_value=[self.guest]),patch.object(self.w,'device',return_value=None),patch.object(self.w,'nm'):
   self.assertEqual(self.w.handle({'action':'forget','uuid':'guest1'})['removed_profiles'],1)
 def test_ap_unknown_and_wrong_adapter_are_protected(self):
  rows=[self.ap,dict(self.guest,mac=self.w.AP_MAC)]
  with patch.object(self.w,'profiles',return_value=rows),patch.object(self.w,'nm') as nm:
   for ident in [self.w.AP,'missing','guest1']:
    with self.assertRaises(RuntimeError):self.w.handle({'action':'forget','uuid':ident})
   nm.assert_not_called()
  self.assertFalse(self.w.FORGOTTEN.exists())
 def test_unexpected_forget_parameters_rejected(self):
  with self.assertRaises(RuntimeError):self.w.handle({'action':'forget','uuid':'guest1','command':'anything'})
 def test_regenerated_profile_is_removed_by_guard_and_hidden(self):
  self.w.save_forgotten({'Example Guest'})
  rows=[self.ap,self.guest,self.dmz]
  with patch.object(self.w,'profiles',return_value=rows),patch.object(self.w,'device',return_value=None),patch.object(self.w,'status',return_value={}),patch.object(self.w,'nm') as nm:
   self.assertEqual(self.w.handle({'action':'saved'}),[dict(uuid='dmz',ssid='DMZ')])
   self.w.ensure()
   nm.assert_called_once_with('connection','delete','guest1')
 def test_explicit_connect_restores_forgotten_network(self):
  self.w.save_forgotten({'Example Guest'})
  with patch.object(self.w,'profiles',return_value=[]),patch.object(self.w,'device',return_value='wlan1'),patch.object(self.w,'status',return_value={}),patch.object(self.w,'nm'):
   self.w.handle({'action':'connect','ssid':'Example Guest','password':'testing123'})
  self.assertEqual(self.w.forgotten(),set())
 def test_preferred_network_beats_other_client_profiles(self):
  with patch.object(self.w,'profiles',return_value=[self.ap,self.guest,self.dmz]),patch.object(self.w,'device',return_value='wlan1'),patch.object(self.w,'status',return_value={}),patch.object(self.w,'nm') as nm:
   self.w.handle({'action':'saved_connect','uuid':'dmz'})
  calls=[c.args for c in nm.call_args_list]
  self.assertIn(('connection','modify','guest1','connection.autoconnect-priority','20'),calls)
  self.assertIn(('connection','modify','dmz','connection.autoconnect-priority','100'),calls)
  self.assertFalse(any(self.w.AP in c for c in calls))
 def test_failed_delete_remains_blocked_for_guard_retry(self):
  with patch.object(self.w,'profiles',return_value=[self.guest]),patch.object(self.w,'nm',side_effect=RuntimeError('temporary error')):
   with self.assertRaises(RuntimeError):self.w.handle({'action':'forget','uuid':'guest1'})
  self.assertIn('Example Guest',self.w.forgotten())
 def test_forgotten_saved_connect_cannot_bypass_removal(self):
  self.w.save_forgotten({'Example Guest'})
  with patch.object(self.w,'profiles',return_value=[self.guest]),patch.object(self.w,'device',return_value='wlan1'),patch.object(self.w,'nm') as nm:
   with self.assertRaises(RuntimeError):self.w.handle({'action':'saved_connect','uuid':'guest1'})
   nm.assert_not_called()
if __name__=='__main__':unittest.main()
