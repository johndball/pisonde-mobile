"""Installation-local adapter identities; no personal defaults in distributed code."""
import json,os,re,uuid
from pathlib import Path

def load_hardware():
    path=Path(os.environ.get('PISONDE_HARDWARE','/etc/pisonde-mobile/hardware.json'))
    try:data=json.loads(path.read_text())
    except (OSError,ValueError) as exc:raise RuntimeError('Run configure.py to create valid local hardware settings') from exc
    for key in ('onboard_mac','client_mac'):
        value=str(data.get(key,'' )).upper()
        if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}',value) or int(value[:2],16)&1 or value=='00:00:00:00:00:00':
            raise RuntimeError('Invalid adapter MAC in hardware settings')
        data[key]=value
    if data['onboard_mac']==data['client_mac']:raise RuntimeError('Wi-Fi adapters must be distinct')
    try:data['ap_uuid']=str(uuid.UUID(data['ap_uuid']))
    except (ValueError,KeyError,TypeError) as exc:raise RuntimeError('Invalid AP UUID') from exc
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',str(data.get('sdr_serial',''))):raise RuntimeError('Invalid SDR serial')
    return data
