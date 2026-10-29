import importlib.util,unittest
from pathlib import Path
from unittest.mock import patch
class AdapterStatusTests(unittest.TestCase):
 def setUp(self):
  spec=importlib.util.spec_from_file_location('mac_broker',Path(__file__).parent/'wifi_control.py');self.w=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.w)
 def test_connected_physical_adapter_mapping(self):
  with patch.object(self.w,'device',side_effect=lambda mac:'wlan0' if mac==self.w.ONBOARD else 'wlan1'),patch.object(self.w,'nm',return_value='connected'):
   s=self.w.status()
  self.assertEqual(s['ap_mac'],'02:00:00:00:00:01');self.assertEqual(s['client_mac'],'02:00:00:00:00:02')
  self.assertEqual(s['ap_interface'],'wlan0');self.assertEqual(s['client_interface'],'wlan1')
 def test_unplugged_linksys_retains_configured_mac(self):
  with patch.object(self.w,'device',side_effect=lambda mac:'wlan0' if mac==self.w.ONBOARD else None),patch.object(self.w,'nm',return_value='connected'):
   s=self.w.status()
  self.assertEqual(s['ap_interface'],'wlan0');self.assertIsNone(s['client_interface']);self.assertFalse(s['usb_present']);self.assertEqual(s['client_mac'],self.w.USB)

 def test_ip_addresses_follow_physical_roles_and_missing_interface(self):
  raw='GENERAL.STATE:100 (connected)\nGENERAL.CONNECTION:Field hotspot\nGENERAL.CON-UUID:saved\nIP4.ADDRESS[1]:192.0.2.20/24\nIP4.GATEWAY:192.0.2.1'
  def nm(*args,**kwargs):
   if args[:2]==('-g','IP4.ADDRESS'):return '192.168.73.1/24'
   if args[:2]==('-g','GENERAL.STATE'):return '100 (connected)'
   return raw
  with patch.object(self.w,'device',side_effect=lambda mac:'wlan0' if mac==self.w.AP_MAC else 'wlan1'),patch.object(self.w,'nm',side_effect=nm):
   result=self.w.status()
  self.assertEqual(result['ap_ipv4'],['192.168.73.1/24']);self.assertEqual(result['client_ipv4'],['192.0.2.20/24'])
  with patch.object(self.w,'device',return_value=None):result=self.w.status()
  self.assertEqual(result['ap_ipv4'],[]);self.assertEqual(result['client_ipv4'],[])
