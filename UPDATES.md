# Automatic application updates

PiSonde Mobile 0.3.17 uses an Updates page and a root-owned systemd worker. No GitHub token is needed for this public repository.

## Fresh installations

Version 0.3.16 and newer integrate public-key enrollment into `install.sh`. The installer verifies the version-specific signed manifest and application bytes, creates the configuration directory as root:root mode 0755, and enrolls the public key. Standard hardware/AP configuration follows; no additional enrollment command is required. See [INSTALL.md](INSTALL.md).

## Existing hardware: one-time signed enrollment

Keep the Pi on stable power with Internet access. Installation briefly stops reception; it does not reboot the Pi. First follow [SIGNED-UPDATES.md](SIGNED-UPDATES.md) to verify the publisher fingerprint and enrollment installer independently.

The enrolled updater reads only `updates/signed-stable.json`, requires an Ed25519 signature from the locally trusted public key, and checks repository/channel, expiry, monotonic metadata sequence and payload identity. It then verifies all application file hashes against the signed immutable source commit. There is no unsigned fallback. Existing 0.3.14 devices stay on the legacy channel until enrollment succeeds.

For the original installation, setup extracts existing adapter identities from local Python constants into a local hardware configuration. It never rewrites saved NetworkManager profiles or passwords. Existing scanner settings, upload identity, reception database and logs remain in place. If it cannot safely identify the installed hardware configuration, setup stops for review.

Use a supported 0.3.10 installation with the existing Python virtual environment and `pisonde-mobile` account. This path does not install missing application dependencies or migrate arbitrary forks. The full fresh-card installer remains separate.

## Dashboard controls

Open **http://192.168.73.1/updates** or select **Updates** from the receiver page.

- **Install during scheduled maintenance window:** choose a two-hour window in 24-hour time: 0100–0300, **0300–0500**, 0500–0700, 0700–0900, continuing through 2300–0100. All times use the Pi's local timezone.
- **If update window missed, install at next reboot:** allows installation outside the window during the next boot's worker run, but only when an update was already known before a selected window ended. It also works on its own, using the selected window to define what was missed. A version first discovered after boot does not qualify for that boot. Failed attempts retain eligibility for retry.
- **Immediately install pending updates when a new version is available:** installs as soon as a check detects/stages an update, including **Check now**, regardless of the selected window. Saving this option also queues a worker run for an already pending update.
- **Save update settings:** saves all three options and the window together. Unsaved selections survive status refreshes.
- **Check now:** refreshes/stages the stable version; installs only if immediate installation is enabled.
- **Install now:** installs immediately regardless of the three options. Reception pauses briefly.

Any combination is allowed. Immediate installation takes priority; otherwise scheduled and missed-window installation are alternative opportunities. With all three unchecked, only manual installation is allowed. Existing automatic-on settings migrate to scheduled **0300–0500**, with the two new options off. An existing automatic-off setting keeps all three off. No automatic Pi reboot is performed.

The systemd worker runs about ten minutes after boot (plus up to five minutes of random delay), hourly near :15 to evaluate the selected window, and on persistent timer catch-up. An hourly or catch-up event can run earlier in startup. It contacts GitHub about every six hours, reusing the staged manifest between checks. **Check now** always requests a fresh manifest. An offline check is retried at later worker runs; a fully downloaded update can be installed without downloading it again. Newly discovered updates require Internet for download. Confirm the Pi's timezone with `timedatectl`.

Installed/available versions, last check, last successful installation and errors appear on the page. Status refreshes every two seconds without reloading the page, with connection/worker timeouts. Manual installation and automatic installation share the same backup, integrity and health checks.

## Recovery and limits

Only the explicit application-file manifest is applied. The updater does not run the fresh-install script, modify OS packages, rebuild decoders, install new Python dependencies, perform database schema migrations or change receiver service definitions (apart from the existing update-recovery dependency). Releases needing those changes require a separate reviewed installer. Ordinary `main` commits do not deploy: a publisher must sign and publish `updates/signed-stable.json` to a tested immutable source commit with compatibility `app-only-v1` and matching hashes.

Version 0.3.14 also migrates the updater-owned timer once through the root Wi-Fi broker when the new application starts. It does not change Wi-Fi roles or saved profiles. This migration allows existing web-updated devices to use every selectable window without an SSH setup command.

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

Confirm **0.3.17** on the dashboard, the Updates controls match your chosen policy, and sample flow/reception resumes. Select **Check now** and confirm **Up to date**. Reboot during a convenient maintenance period and confirm the AP and receiver return. Hardware installation is not verified until these device-side checks succeed.

All web-changing actions require the existing CSRF token and go through a narrow root broker with fixed operation names. The local dashboard uses HTTP: keep it on a trusted network and do not expose it directly to the Internet.

## Release notes

Read [CHANGELOG.md](CHANGELOG.md) or the repository's GitHub Releases page. Tags/Releases document tested source snapshots; they do not independently change the device's stable channel. A repository workflow publishes the reviewed version catalog under `releases/` and refuses to move an existing version tag.

GUI restart/shutdown requests are rejected while an update is running, queued or awaiting interrupted-update recovery. Updates are refused while a power action is pending. These interlocks apply to GUI/broker actions; an administrator or physical power removal can still interrupt a device.
