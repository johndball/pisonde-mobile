#!/usr/bin/env bash
# Initial trust still requires verifying this bootstrap or the enrollment installer independently.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Run with sudo bash bootstrap-updates.sh'; exit 1; }
WORK="$(mktemp -d /tmp/pisonde-signed-setup.XXXXXXXX)"
trap 'rm -rf "$WORK"' EXIT
python3 - "$WORK" <<'PY'
import hashlib,sys,urllib.request
from pathlib import Path
url='https://raw.githubusercontent.com/johndball/pisonde-mobile/main/enroll-signed-updates.py'
request=urllib.request.Request(url,headers={'User-Agent':'PiSonde-Signed-Setup/1','Cache-Control':'no-cache'})
with urllib.request.urlopen(request,timeout=30) as response:
    if not response.geturl().startswith('https://raw.githubusercontent.com/johndball/pisonde-mobile/'):raise SystemExit('Unexpected installer redirect')
    data=response.read(65537)
if len(data)>65536 or hashlib.sha256(data).hexdigest()!='602d94c2f2575ed666cccbce2b3a8c4e6c7363e3bf59bcf6c3ee272fa57ce76c':raise SystemExit('Enrollment installer checksum mismatch; nothing executed')
(Path(sys.argv[1])/'enroll.py').write_bytes(data)
PY
python3 "$WORK/enroll.py"
