#!/usr/bin/env bash
# One-time setup for an existing PiSonde Mobile device; no secrets are embedded.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Run with sudo bash bootstrap-updates.sh'; exit 1; }
WORK="$(mktemp -d /tmp/pisonde-update-setup.XXXXXXXX)"
trap 'rm -rf "$WORK"' EXIT
python3 - "$WORK" <<'PY'
import hashlib,json,re,sys,urllib.request
from pathlib import Path
base='https://raw.githubusercontent.com/johndball/pisonde-mobile/'
def fetch(url,limit):
    request=urllib.request.Request(url,headers={'User-Agent':'PiSonde-Mobile-Setup/1'})
    with urllib.request.urlopen(request,timeout=30) as response:
        if not response.geturl().startswith(base):raise SystemExit('Unexpected download redirect')
        data=response.read(limit+1)
    if len(data)>limit:raise SystemExit('Download too large')
    return data
manifest=json.loads(fetch(base+'main/updates/stable.json',65536))
commit=manifest.get('commit','');digest=manifest.get('files',{}).get('update_manager.py','')
if manifest.get('schema')!=1 or manifest.get('compatibility')!='app-only-v1' or not re.fullmatch('[0-9a-f]{40}',commit) or not re.fullmatch('[0-9a-f]{64}',digest):raise SystemExit('Invalid stable manifest')
source=fetch(base+commit+'/update_manager.py',262144)
if hashlib.sha256(source).hexdigest()!=digest:raise SystemExit('Updater checksum mismatch')
compile(source,'update_manager.py','exec')
(Path(sys.argv[1])/'update_manager.py').write_bytes(source)
PY
python3 "$WORK/update_manager.py" bootstrap
