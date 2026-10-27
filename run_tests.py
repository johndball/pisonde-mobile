#!/usr/bin/env python3
"""Offline regression suite. Hardware and network operations are mocked."""
import json,os,sys,tempfile,unittest
from pathlib import Path
with tempfile.TemporaryDirectory() as temp:
    config=Path(temp)/'hardware.json'
    config.write_text(json.dumps(dict(onboard_mac='02:00:00:00:00:01',client_mac='02:00:00:00:00:02',ap_uuid='11111111-2222-4333-8444-555555555555',sdr_serial='00000001')))
    os.environ['PISONDE_HARDWARE']=str(config)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(Path(__file__).parent),pattern='test_*.py'))
    sys.exit(not result.wasSuccessful())
