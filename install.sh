#!/usr/bin/env bash
# Fresh-install public edition. Does not migrate an existing field installation.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Run with sudo bash install.sh'; exit 1; }
[ "$(dpkg --print-architecture)" = arm64 ] || { echo 'Requires Raspberry Pi OS Lite 64-bit (arm64).'; exit 1; }
. /etc/os-release
[ "${VERSION_CODENAME:-}" = trixie ] || { echo 'This installer targets Trixie. No changes made.'; exit 1; }
DEST=/opt/pisonde-mobile
[ ! -e "$DEST" ] || { echo 'Existing installation found. Use a fresh SD card; this installer will not overwrite it.'; exit 1; }
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[ ! -e /etc/pisonde-mobile/hardware.json ] && [ ! -e /var/lib/pisonde-mobile/receptions.sqlite3 ] || { echo 'Existing configuration/history found; fresh installation refused.'; exit 1; }
[ ! -L /etc/pisonde-mobile ] || { echo 'Configuration directory must not be a symlink.'; exit 1; }
WORK="$(mktemp -d /tmp/pisonde-fresh.XXXXXXXX)"
trap 'rm -rf "$WORK"' EXIT
apt-get update
apt-get install -y python3 openssl ca-certificates
# Verify the publisher signature and application bytes before installing them.
python3 "$SOURCE/fresh_install.py" preflight "$SOURCE" "$WORK/release.json"
apt-get install -y python3-venv python3-dev git build-essential pkg-config libffi-dev libusb-1.0-0-dev librtlsdr-dev rtl-sdr usbutils sox direwolf network-manager ethtool iw libfftw3-dev
getent group plugdev >/dev/null || groupadd --system plugdev
id pisonde-mobile >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/pisonde-mobile --shell /usr/sbin/nologin pisonde-mobile
usermod -a -G plugdev pisonde-mobile
# Set ownership explicitly even if the configuration directory already exists.
install -d -o root -g root -m 0755 "$DEST" "$DEST/bin" /etc/pisonde-mobile
install -m 0644 "$SOURCE"/*.py "$DEST/"
install -m 0644 "$SOURCE/requirements.txt" "$DEST/"
install -d -o pisonde-mobile -g pisonde-mobile -m 0750 /var/lib/pisonde-mobile /var/log/pisonde-mobile
install -d -o root -g pisonde-mobile -m 0750 /var/backups/pisonde-mobile
install -d -o pisonde-mobile -g pisonde-mobile -m 0750 /var/backups/pisonde-mobile/history
clone_pinned() {
    git init "$DEST/$1"
    git -C "$DEST/$1" remote add origin "$2"
    git -C "$DEST/$1" fetch --depth 1 origin "$3"
    git -C "$DEST/$1" checkout --detach FETCH_HEAD
    test "$(git -C "$DEST/$1" rev-parse HEAD)" = "$3"
}
clone_pinned horusdemodlib https://github.com/projecthorus/horusdemodlib.git 8cc43de5567ce82132d46c5e37897f9bcc09a755
clone_pinned radiosonde_auto_rx https://github.com/projecthorus/radiosonde_auto_rx.git 53d03c72ad18ce4357c0cedd1f4acf2bf1efb36e
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/python" -m pip install -r "$DEST/requirements.txt" "$DEST/horusdemodlib"
"$DEST/venv/bin/python" -m pip check
make -C "$DEST/radiosonde_auto_rx/demod/mod" rs41mod dfm09mod
make -C "$DEST/radiosonde_auto_rx/scan" dft_detect
install -m 0755 "$DEST/radiosonde_auto_rx/demod/mod/rs41mod" "$DEST/radiosonde_auto_rx/demod/mod/dfm09mod" "$DEST/radiosonde_auto_rx/auto_rx/"
install -m 0755 "$DEST/radiosonde_auto_rx/scan/dft_detect" "$DEST/radiosonde_auto_rx/auto_rx/"
# A separate capped survey executable; system RTL-SDR tools remain unchanged.
cc -O2 -std=gnu99 -I"$SOURCE/rate-survey/include" $(pkg-config --cflags librtlsdr libusb-1.0) \
    "$SOURCE/rate-survey/rtl_power.c" "$SOURCE/rate-survey/convenience/convenience.c" \
    -o "$DEST/bin/rtl_power" $(pkg-config --libs librtlsdr libusb-1.0) -lm -pthread
install -d -m 0755 /etc/modprobe.d /etc/udev/rules.d
cat > /etc/modprobe.d/pisonde-rtl-sdr.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832_sdr
blacklist rtl2832
EOF
cat > /etc/udev/rules.d/60-pisonde-rtl-sdr.rules <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="0bda", ATTR{idProduct}=="2838", MODE="0660", GROUP="plugdev"
EOF
cat > /etc/pisonde-mobile/aprs.conf <<'EOF'
ADEVICE stdin null
ARATE 24000
ACHANNELS 1
CHANNEL 0
MYCALL N0CALL
MODEM 1200
AGWPORT 0
KISSPORT 0
EOF
chmod 0644 /etc/pisonde-mobile/aprs.conf
udevadm control --reload-rules
install -m 0644 "$SOURCE"/systemd/* /etc/systemd/system/
systemctl daemon-reload
"$DEST/venv/bin/python" -m pip freeze > "$DEST/installed-python-versions.txt"
python3 "$SOURCE/fresh_install.py" enroll "$SOURCE" "$WORK/release.json"
echo 'Installed signed PiSonde Mobile 0.3.21. Services are not started yet.'
echo 'Public key enrolled automatically; no separate enrollment command is needed.' 
echo 'Reboot to release the TV driver, then use Ethernet or a local console:'
echo 'sudo python3 /opt/pisonde-mobile/configure.py'
echo 'If installation failed partway, inspect the error; do not remove an existing installation blindly.'
