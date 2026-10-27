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
