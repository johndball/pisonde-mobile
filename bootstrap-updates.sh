#!/usr/bin/env bash
# One-time setup for an existing PiSonde Mobile device; no secrets are embedded.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Run with sudo bash bootstrap-updates.sh'; exit 1; }
WORK="$(mktemp -d /tmp/pisonde-update-setup.XXXXXXXX)"
trap 'rm -rf "$WORK"' EXIT
python3 - "$WORK" <<'PY'
import base64,hashlib,json,re,sys,time,urllib.request
from pathlib import Path
base='https://raw.githubusercontent.com/johndball/pisonde-mobile/'
api='https://api.github.com/repos/johndball/pisonde-mobile/'
def fetch(url,limit):
    request=urllib.request.Request(url,headers={'User-Agent':'PiSonde-Mobile-Setup/1','Cache-Control':'no-cache'})
    with urllib.request.urlopen(request,timeout=30) as response:
        expected=api if url.startswith(api) else base
        if not response.geturl().startswith(expected):raise SystemExit('Unexpected download redirect')
        data=response.read(limit+1)
    if len(data)>limit:raise SystemExit('Download too large')
    return data
entry=json.loads(fetch(api+'contents/updates/stable.json?ref=main&setup='+str(time.time_ns()),131072))
if entry.get('encoding')!='base64' or entry.get('path')!='updates/stable.json':raise SystemExit('Invalid channel API response')
manifest=json.loads(base64.b64decode(entry['content']))
if not re.fullmatch(r'\d+\.\d+\.\d+',str(manifest.get('version',''))) or tuple(map(int,manifest['version'].split('.')))<(0,3,13):raise SystemExit('Stable update publication is incomplete; retry shortly')
commit=manifest.get('commit','');digest=manifest.get('files',{}).get('update_manager.py','')
if manifest.get('schema')!=1 or manifest.get('compatibility')!='app-only-v1' or not re.fullmatch('[0-9a-f]{40}',commit) or not re.fullmatch('[0-9a-f]{64}',digest):raise SystemExit('Invalid stable manifest')
source=fetch(base+commit+'/update_manager.py',262144)
if hashlib.sha256(source).hexdigest()!=digest:raise SystemExit('Updater checksum mismatch')
compile(source,'update_manager.py','exec')
root=Path(sys.argv[1]);(root/'update_manager.py').write_bytes(source);(root/'manifest.json').write_text(json.dumps(manifest))
PY
python3 "$WORK/update_manager.py" bootstrap "$WORK/manifest.json"
