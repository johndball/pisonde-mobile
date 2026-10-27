"""Sample-output watchdog and fixed-identity USB reset for PiSonde Mobile.

No packet timing is used: an idle radio must still deliver samples. The pipeline
counts only sample bytes forwarded to the decoder, excluding all stderr logs.
"""
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import time
import threading

from hardware import load_hardware
SERIAL = load_hardware()['sdr_serial']
USB_ROOT = Path('/sys/bus/usb/devices')
RESET_HISTORY = Path('/run/pisonde-sdr-reset-history.json')

class RadioFailure(RuntimeError):
    pass

class FlowMonitor:
    def __init__(self, pid, counter, clock=time.monotonic,
                 timeout=6.0, minimum=16384):
        self.pid, self.clock, self.counter = pid, clock, counter
        self.timeout, self.minimum = timeout, minimum
        self.started = self.last_progress = clock()
        self.baseline = counter(pid)
        self.ready = False
        self.progressed = False
        self.bytes_seen = 0

    def check(self):
        now, count = self.clock(), self.counter(self.pid)
        self.progressed = False
        if count < self.baseline:
            raise RadioFailure('SDR output counter changed unexpectedly')
        if count - self.baseline >= self.minimum:
            self.bytes_seen += count - self.baseline
            self.baseline, self.last_progress, self.ready = count, now, True
            self.progressed = True
        if now - self.last_progress >= self.timeout:
            raise RadioFailure('SDR sample output stalled for 6 seconds')
        return self.ready

class FreshnessMonitor:
    """Conservative exact-repeat check; no packet or signal-strength heuristic.

    Keep 32 KiB, check at most twice a second, and require six seconds of
    bit-identical periodic output. This detects the field-observed 1880-byte
    PCM loop. A lock publishes an atomic snapshot to the scanner thread.
    """
    def __init__(self, clock=time.monotonic, timeout=6.0):
        self.clock, self.timeout = clock, timeout
        self.buffer = b''
        self.next_check = float('-inf')
        self.since = None
        self.fresh = False
        self.period = None
        self.fault = ''
        self.lock = threading.Lock()

    def feed(self, data):
        self.buffer = (self.buffer + data)[-32768:]
        now = self.clock()
        if len(self.buffer) < 32768 or now < self.next_check:
            return
        self.next_check = now + .5
        raw = self.buffer
        period = raw.find(raw[:128], 1, 8192 + 128)
        if period <= 0 or period > 8192 or raw[period:] != raw[:-period]:
            period = None
        with self.lock:
            self.period = period
            self.fresh = period is None
            if period is None:
                self.since = None
            else:
                if self.since is None:
                    self.since = now
                if now - self.since >= self.timeout:
                    self.fault = ('SDR samples repeat exactly for 6 seconds; '
                                  'restart/reset required; unplug RTL-SDR if persistent')

    def check(self):
        with self.lock:
            if self.fault:
                raise RadioFailure(self.fault)
            return self.fresh, self.period

class Recovery:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.failures = self.restarts = self.resets = 0
        self.last_reset = float('-inf')
        self.last_fault = ''

    def healthy(self):
        self.failures = 0

    def fault(self, message):
        self.failures += 1
        self.last_fault = str(message)
        self.restarts += 1
        # One pipeline restart first; later failures escalate at most every 120 s.
        if self.failures >= 2 and self.clock() - self.last_reset >= 120:
            self.last_reset = self.clock()
            return 'reset'
        return 'restart' if self.failures == 1 else 'wait'

def request_reset():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(40)
        sock.connect('/run/pisonde-control/control.sock')
        sock.sendall(b'{"action":"sdr_reset"}\n')
        with sock.makefile('rb') as stream:
            raw = stream.readline(8193)
    if len(raw) > 8192:
        raise RadioFailure('Invalid SDR reset response')
    result = json.loads(raw)
    if not result.get('ok'):
        raise RadioFailure(result.get('error', 'SDR reset unavailable'))
    return result['result']

def target_device(root=None):
    matches = []
    for device in (root or USB_ROOT).iterdir():
        try:
            if ((device/'idVendor').read_text().strip().lower() == '0bda'
                and (device/'idProduct').read_text().strip().lower() == '2838'
                and (device/'serial').read_text().strip() == SERIAL):
                bus = int((device/'busnum').read_text())
                number = int((device/'devnum').read_text())
                if not 1 <= bus <= 255 or not 1 <= number <= 127:
                    raise RuntimeError('Invalid SDR USB address')
                matches.append((bus, number))
        except (FileNotFoundError, NotADirectoryError):
            continue
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one RTL-SDR matching configured serial; reconnect the dongle')
    return matches[0]

def reset_device():
    """Runs only in the bounded root helper, after all SDR processes stop."""
    for command in ('rtl_fm', 'rtl_power', 'rtl_sdr', 'rtl_test'):
        result = subprocess.run(['pgrep', '-x', command], capture_output=True, timeout=2)
        if result.returncode != 1:
            raise RuntimeError('SDR processes are still active; USB reset refused')
    bus, number = target_device()
    path = f'/dev/bus/usb/{bus:03d}/{number:03d}'
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISCHR(info.st_mode) or os.major(info.st_rdev) != 189
            or os.minor(info.st_rdev) != (bus-1)*128 + number-1):
            raise RuntimeError('USB device node verification failed')
        # USBDEVFS_RESET = _IO("U", 20), identical on Linux arm64 and x86_64.
        fcntl.ioctl(fd, 0x5514, 0)
    finally:
        os.close(fd)
    return dict(device=path, serial=SERIAL, message='Targeted RTL-SDR USB reset completed')

def broker_reset():
    """Root broker entry: fixed target, bounded child, persistent attempt limits."""
    now = time.monotonic()
    try:
        history = json.loads(RESET_HISTORY.read_text())
        if not isinstance(history, list):
            raise ValueError('Invalid reset history')
        history = [float(t) for t in history if 0 <= now-float(t) < 3600]
    except FileNotFoundError:
        history = []
    if history and now-max(history) < 120:
        raise RuntimeError('SDR reset cooldown active; reconnect the dongle if needed')
    if len(history) >= 3:
        raise RuntimeError('SDR reset limit reached (3 per hour); reconnect the dongle')
    history.append(now)
    fd = os.open(RESET_HISTORY, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(history, stream)
    result = subprocess.run(['/usr/bin/python3', str(Path(__file__).resolve()), '--reset-device'],
                            capture_output=True, text=True, timeout=12)
    if result.returncode:
        raise RuntimeError('Targeted SDR USB reset failed; reconnect the dongle')
    return json.loads(result.stdout)

if __name__ == '__main__':
    import sys
    if os.geteuid() != 0 or sys.argv[1:] != ['--reset-device']:
        raise SystemExit('This fixed-target reset helper is called by the local service only.')
    print(json.dumps(reset_device()))
