# Install PiSonde Mobile 0.3.16 — public packaging revision 1

This is a **fresh-install source package**, not an update for an existing PiSonde installation. The receiver/GUI code is based on 0.3.16. Public packaging adds hardware discovery and removes installation-specific identities. **This fresh-SD installer has passed offline checks but has not yet been run end-to-end on a Raspberry Pi.** Keep your working card intact and use a spare card for the first installation.

## Requirements

Use a Raspberry Pi 4 with **Raspberry Pi OS Lite, 64-bit, Debian Trixie**, one onboard Wi-Fi adapter, one supported USB Wi-Fi adapter, one RTL-SDR Blog V4, and Internet access during installation. This installer is deliberately limited to arm64/Trixie. Other receivers, boards and OS versions are not validated by this package.

Use Ethernet or a local keyboard/console for initial setup. Creating the access point changes Wi-Fi roles and disconnects an existing Wi-Fi SSH session. A stable power source and at least several GB of free storage are required for packages and compilation. The application keeps reception history, so leave plenty of room for field use.

## 1. Flash and boot

Flash Raspberry Pi OS Lite 64-bit to a blank microSD card. Set your own hostname, SSH username and password in the imaging tool. Enable SSH if you will administer the Pi remotely. Connect Ethernet, the SDR and USB Wi-Fi adapter, then boot. SSH to the Pi's initial Ethernet address, or use its console.

Do not reuse a published password. The AP password is chosen separately during setup; no password is supplied in the download.

## 2. Get the source and install

On the Pi, using Ethernet or a local console:

```sh
sudo apt-get update
sudo apt-get install -y git
cd ~
git clone --branch v0.3.16 --depth 1 https://github.com/johndball/pisonde-mobile.git
cd pisonde-mobile
sudo bash install.sh
```

Use the tagged release source; a development branch or newer un-published source will fail the signed file checks. You can also download the tagged release ZIP from GitHub, extract it, and run `sudo bash install.sh` from the extracted directory.

Installation first fetches the version-specific signed manifest and verifies the bundled public key and all ten application files. Python/OpenSSL/CA certificates may be installed to perform this check. A missing, expired or mismatched manifest stops application installation. It then downloads dependencies, checks out pinned Horus and radiosonde decoder sources, builds the DFM/RS41/detection executables, and compiles a separate rate-capped survey tool. It does not replace the system RTL-SDR library or start reception yet. After building, it rechecks the installed application bytes, sets `/etc/pisonde-mobile` to root:root mode 0755, and enrolls the root-owned public key automatically. Directory normalization does not recursively change file ownership. Internet access to Debian/Raspberry Pi package mirrors, GitHub and Python package indexes is required.

The installer refuses an existing `/opt/pisonde-mobile` directory. If it fails partway, keep the error output and resolve it before retrying; do not delete an existing working installation to bypass that check. This package does not offer an in-place migration or full installer rollback.

## 3. Reboot, then configure the radios

```sh
sudo reboot
```

Reconnect over Ethernet or use the local console. The reboot applies the DVB/TV-driver blacklist. Then run:

```sh
sudo python3 /opt/pisonde-mobile/configure.py
```

The setup script identifies the onboard radio and USB Wi-Fi radio by their hardware connections and permanent MAC addresses. It reads the attached SDR's serial and generates a new AP profile UUID. You choose and confirm an AP password of 8–63 printable ASCII characters.

The resulting layout is:

| Radio | Role |
| --- | --- |
| Onboard Wi-Fi | `pisondemobile` local access point, 2.4 GHz, gateway `192.168.73.1` |
| USB Wi-Fi | Upstream connection to an Internet hotspot or saved Wi-Fi network |
| RTL-SDR Blog V4 | One shared receiver for sequential scanning/decoding |

Activation runs separately so it can finish after a Wi-Fi SSH disconnect. It starts and enables the receiver, Wi-Fi broker and daily backup timer. If AP activation fails, it attempts to restore the prior Wi-Fi profile bindings. Keep Ethernet or console access until you verify the AP.

The initial setup expects both Wi-Fi adapters and exactly one compatible SDR. After setup, the onboard AP should remain available when the USB Wi-Fi adapter is absent. Avoid using an upstream network whose subnet overlaps `192.168.73.0/24`.

## 4. Open the dashboard and add Internet access

Connect your phone to **pisondemobile**, using your new password, and open:

**http://192.168.73.1**

Port 80 is the persistent service default. There is no `:5001` to add.

Use the **Wi-Fi** page to scan for and connect to your upstream hotspot. Saved upstream profiles are bound to the USB radio. The **Forget** control removes unwanted saved networks; adapter MACs are visible locally for troubleshooting. The upstream connection can be shared with clients of the Pi's AP. An associated Wi-Fi connection does not by itself prove that Internet access works.

The dashboard uses local HTTP. Use a trusted field network and do not expose the dashboard directly to the public Internet. Remote management software is optional and is not included or configured here.

## 5. Configure uploads deliberately

Uploads start **disabled** with the placeholder `N0CALL`. On **Uploads**, enter your own callsign/SSID and explicitly enable uploads. The application refuses to enable the placeholder identity. Receiver coordinates are not published by this build.

| Received data | Destination and status |
| --- | --- |
| APRS | APRS-IS; eligible packets may appear on aprs.fi or SondeHub Amateur through their ingestion paths. A successful socket write is not proof of map ingestion. |
| Horus | SondeHub Amateur. Reserved test payloads use development-only validation. |
| DFM / RS41 | SondeHub weather endpoint. **Custom uploader approval is still required**; this package keeps the `PiSonde-Mobile` identity. Local decoding/history works independently. |

Outbound TCP 14580 is used for the default APRS-IS server. SondeHub uploads use HTTPS/TCP 443. DNS and a correctly synchronized clock are also needed. No inbound Internet port forwarding is required. SondeHub development validation does not create a live map track.

For weather approval, the dashboard's guide links to the current [SondeHub telemetry validation instructions](https://github.com/projecthorus/sondehub-infra/wiki/Sonde-Telemetry-Validation) and provides JSONL/report exports. Approval is not bundled with the software. Old telemetry is retained locally rather than re-dated and presented as live data.

## 6. Check the installation

```sh
systemctl is-active pisonde-mobile.service pisonde-wifi-control.service pisonde-history-backup.timer
nmcli device status
curl -fsS http://127.0.0.1/api/status
sudo /opt/pisonde-mobile/venv/bin/python /opt/pisonde-mobile/verify_recovery.py --field-only
```

The field check validates the current history database, exports, adapter bindings, services and a fresh backup. If it fails, inspect the reported check rather than declaring the installation complete. Services should be active, the onboard radio should serve the AP, and the UI should show a scan profile with sample flow while decoding candidates. In the scanner, check several status updates; counters changing alone do not demonstrate packet decoding.

Select a known local APRS frequency or another known source. For an APRS audio-over-radio test, select **APRS** as the decoder and match the transmitter frequency exactly; Horus 4FSK audio needs the Horus decoder. Confirm a newly timestamped packet appears, then verify local coordinates and any eligible upload separately. This public download does not include recorded private transmissions or a synthetic live-map balloon WAV.

For amateur-band transmission tests, use your own authorized station setup and frequency. Never transmit a test signal in the weather-sonde band to check reception.

Reboot once more and confirm the AP returns and scanning resumes without clicking Resume scan. A saved manual lock/stopped state is cleared at service startup; saved scanning profiles remain selected. Check the dashboard still works after removing only the USB Wi-Fi adapter. Upstream connectivity will be lost while that adapter is absent.

The full recovery test (`verify_recovery.py` without `--field-only`) deliberately interrupts receiver subprocesses. It is optional and should only be run during a maintenance window after reading its help. It does not substitute for a real reception test.

## Optional shutdown button

For the Pi 4, with power disconnected, connect a **normally open momentary switch** between **GPIO3, physical header pin 5**, and **GND, physical pin 6**. Use header pin numbers, not GPIO numbering, when locating the pins. Add this line to `/boot/firmware/config.txt` under an applicable `[all]` section, then reboot:

```ini
dtoverlay=gpio-shutdown,gpio_pin=3,active_low=1,gpio_pull=up,debounce=200
```

The installer does not change GPIO settings. Do not combine this with another use of GPIO3, such as an I2C device, without reviewing the conflict. Test the button and confirm shutdown has completed before removing power. A halt does not physically disconnect the power supply.

## Storage, maintenance and privacy

Local history lives under `/var/lib/pisonde-mobile`. A daily checked SQLite backup retains the latest and previous copies under `/var/backups/pisonde-mobile/history`; these share the same card and do not replace an off-device backup. Use CSV/GPX exports for recovery records you cannot afford to lose. There is no promise of surviving arbitrary power removal.

Hardware identities are created locally in `/etc/pisonde-mobile/hardware.json`. Wi-Fi credentials remain in NetworkManager's local profiles. Runtime files, saved networks, upload settings, logs, databases and backups can contain private information. **Do not redistribute your installed `/opt`, `/etc`, `/var/lib` or backup directories as a public release.** Share the clean source ZIP instead.

Dependencies: the two decoder repositories are commit-pinned in `install.sh`; Python/OS packages are resolved at installation time. The installed Python version list is recorded in `/opt/pisonde-mobile/installed-python-versions.txt`. This is a source installer, not a fully reproducible SD-card image.

## Application updates

Fresh setup installs the background update timer and Updates page. See [UPDATES.md](UPDATES.md) for automatic-install settings, maintenance timing and recovery limits. Existing devices use that guide’s one-time setup instead of rerunning this fresh installer.

## Signed update enrollment

Fresh installations of 0.3.16 and newer enroll the publisher public key during `install.sh`; no separate enrollment command is needed. Hardware setup verifies the enrolled key before changing Wi-Fi. After configuration, use Updates → Check now and confirm that signatures are required and the key is enrolled. Existing enrolled devices update normally through the GUI. Unenrolled older installations still use the one-time verified enrollment installer described in [SIGNED-UPDATES.md](SIGNED-UPDATES.md).
