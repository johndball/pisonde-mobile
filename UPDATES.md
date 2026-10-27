# Automatic application updates

PiSonde Mobile 0.3.12 adds an Updates page and a root-owned systemd worker. No GitHub token is needed for this public repository.

## Existing hardware: one-time setup

Keep the Pi on stable power with Internet access. Installation briefly stops reception; it does not reboot the Pi. From SSH:

```sh
curl --fail --location --max-time 60 https://raw.githubusercontent.com/johndball/pisonde-mobile/main/bootstrap-updates.sh -o /tmp/pisonde-updates.sh && sudo bash /tmp/pisonde-updates.sh
```

This fetches the stable manifest over HTTPS, verifies the updater's SHA-256, then downloads the listed application files from the manifest's immutable source commit and checks each hash. The repository owner's stable-channel publishing permission is the trust boundary; hashes detect corrupt or mismatched content, not a compromised publisher. No private configuration is uploaded.

For the original installation, setup extracts existing adapter identities from local Python constants into a local hardware configuration. It never rewrites saved NetworkManager profiles or passwords. Existing scanner settings, upload identity, reception database and logs remain in place. If it cannot safely identify the installed hardware configuration, setup stops for review.

Use a supported 0.3.10 installation with the existing Python virtual environment and `pisonde-mobile` account. This path does not install missing application dependencies or migrate arbitrary forks. The full fresh-card installer remains separate.

## Dashboard controls

Open **http://192.168.73.1/updates** or select **Updates** from the receiver page.

- **Automatic updates:** checked by default on first setup. Allows installation from 03:00 to 05:00 in the Pi's local timezone. Unchecking preserves background checks and manual installation.
- **Check now:** checks and stages a newer stable version without installing it.
- **Install now:** checks, downloads and installs a newer stable version immediately; reception pauses briefly.
- Installed/available versions, last check, last successful installation and errors appear on the page. It refreshes without reloading the full page.

The systemd timer checks about ten minutes after boot, every six hours thereafter and daily around 03:15. Checks/installations require Internet at that time. Missed checks are retried at later timer runs; an offline Pi continues receiving with its existing application. The checkbox does not disable checking. Confirm the Pi's timezone with `timedatectl` before relying on the maintenance window.

## Recovery and limits

Only the explicit application-file manifest is applied. The updater does not run the fresh-install script, modify OS packages, rebuild decoders, install new Python dependencies, change service definitions during routine updates or perform database schema migrations. Releases needing those changes require a separate reviewed installer. Ordinary `main` commits do not deploy: a publisher must move `updates/stable.json` to a tested immutable source commit with compatibility `app-only-v1` and matching hashes.

Before installation, the worker stages and compiles every Python source file, verifies its version/hashes, checks space, makes a checked SQLite backup and stores previous source files. A durable pending-transaction record is written before stopping services. Source files are replaced atomically, then the receiver and Wi-Fi broker restart. Service state, the application's API version and SQLite integrity must pass health checks, or the previous application is restored. This does not prove RF reception or successful Internet telemetry delivery.

At boot, a recovery unit runs before either application service and rolls back a pending interrupted transaction. SQLite history is not replaced during an application rollback. Three successful-update rollback snapshots are retained under `/var/backups/pisonde-updater`; these share the SD card and do not replace off-device backups. A failure during recovery, a failed SD card or repeated power loss can still require manual repair.

The hardware setup, update settings, saved networks and reception history are local. Root-owned updater state lives in `/var/lib/pisonde-updater`; automatic-install settings are in `settings.json`. Do not redistribute these directories. Routine releases should keep the pending-transaction format and app-only compatibility backward compatible.

## Verification and troubleshooting

After setup:

```sh
systemctl is-active pisonde-mobile.service pisonde-wifi-control.service pisonde-update.timer
systemctl list-timers pisonde-update.timer --no-pager
sudo journalctl -u pisonde-update-run.service -u pisonde-update-install.service -n 40 --no-pager
```

Confirm **0.3.12** on the dashboard, the Updates checkbox is checked, and sample flow/reception resumes. Select **Check now** and confirm **Up to date**. Reboot during a convenient maintenance period and confirm the AP and receiver return. Hardware installation is not verified until these device-side checks succeed.

All web-changing actions require the existing CSRF token and go through a narrow root broker with fixed operation names. The local dashboard uses HTTP: keep it on a trusted network and do not expose it directly to the Internet.
