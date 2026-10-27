# Changelog

Application versions, stable-channel deployments and GitHub Releases are related but distinct. An enrolled device installs only the immutable source snapshot authenticated by `updates/signed-stable.json` (legacy devices use `updates/stable.json`); publishing a tag alone does not deploy code. GitHub Releases are available from the repository's Releases page.

## 0.3.16 — 2026-10-27

- Integrate signed manifest verification and public-key enrollment into fresh installation. New users complete normal install/hardware setup without a separate enrollment command.
- Explicitly create or normalize the configuration directory as root:root mode 0755, fixing service-owned directory enrollment failures. Preserve ownership of individual existing files and reject symlink directories.
- Verify version-specific signed metadata and all application source bytes before copying; recheck installed files before enrolling trust. Refuse configured devices/history and conflicting trust keys.
- Require a valid enrolled key before fresh hardware/AP configuration changes Wi-Fi roles.
- Publish version-specific signed metadata alongside the stable channel, allowing tagged source installation independently of later stable releases.
- Existing enrolled devices retain GUI/automatic updates and local settings/history. The app-only updater does not rerun the fresh installer.
- Pass 194 offline regression checks, including real signature/enrollment tests and directory-permission normalization. A fresh-card arm64/Trixie installation remains a separate hardware validation task.

## 0.3.15 — 2026-10-27

- Add a separate signed channel while preserving the 0.3.14 legacy channel and all historical releases.
- Add local encrypted Ed25519 key generation and manifest signing tools; no production private keys are created or held by CI.
- Verify signed manifests against a root-owned, independently enrolled public key using OpenSSL. Check repository/channel, validity, metadata sequence and signed payload identity before staging/applying.
- Reject unsigned, tampered, wrong-key, expired and replayed metadata. Preserve existing per-file integrity, backup and rollback checks.
- Add 179 regression checks, including real Python/OpenSSL interoperability, tampering/replay rejection and enrollment ordering. The first publisher signature was verified before publication; hardware enrollment and RF validation remain separate.

## 0.3.14 — 2026-10-27

- Replace the single automatic-install checkbox with three combinable options: a selectable maintenance window, next-boot installation after a missed window, and immediate installation when a version is detected.
- Display selectable two-hour windows in 24-hour Pi local time, including 2300–0100. Preserve existing enabled/disabled installation settings; new behaviors default off.
- Evaluate policy hourly while keeping GitHub checks about six-hourly. Preserve first-discovery and boot identity across restarts so missed-window installation does not apply to a newly discovered version at that boot.
- Save all settings together; retain unsaved selections through status polling. Immediate mode also applies to manual checks and queues a worker when enabled.
- Migrate the updater-owned systemd timer on the existing device through the root broker after installation, without a separate SSH bootstrap.
- Add regression checks for all eight option combinations, local-time/midnight boundaries, reboot eligibility/retries, schema migration, validation and the GUI API.
- Device-side schedule/reboot validation remains necessary; offline tests cannot establish field reliability.

## 0.3.13 — 2026-10-27

- Read the mutable stable manifest through GitHub's contents API rather than a potentially stale raw-content response. Immutable application downloads remain hash-verified.
- Pass the same validated manifest through bootstrap so installer selection cannot change between downloads. Reject a channel older than the bootstrap version.
- Track queued/started/completed operations with IDs and explicit outcomes. Report downloads, restarts, verification, completion and failures on the Updates page.
- Refresh update status every two seconds without reloading the page. Bound requests and queued operations with connection/worker timeout feedback; prevent duplicate requests.
- Show the running application version when it differs from the source on disk, and disable caching for update pages/status.
- Add a changelog, per-version release notes and an idempotent tag/Release publisher workflow.
- Device-side verification is still required after installation; offline checks do not establish RF reception or field reliability.

## 0.3.12 — 2026-10-27

- Replace the invalid `Path.chown()` bootstrap call with `os.chown()`.
- Exercise the bootstrap ownership/setup path in regression tests.
- Request fresh stable manifests rather than relying on default cache behavior.
- The affected first attempt failed before service interruption or application replacement.

## 0.3.11 — 2026-10-27

- Add stable-channel application updates, a systemd timer and local Updates controls.
- Check about every six hours; install automatically between 03:00 and 05:00 local time when enabled.
- Preserve hardware/network/upload settings and reception history. Stage and verify Python files, back up the database and application, check service/API/database health, and attempt rollback on failure.
- Add boot recovery for interrupted transactions. Do not reboot the Pi automatically.
- Known issue: initial updater bootstrap used an unsupported ownership method. Superseded by later versions; this historical version is marked prerelease.

## 0.3.10 — 2026-10-27 public repository baseline

- Publish the sanitized 0.3.10 receiver, phone dashboard and fresh-install source.
- Include APRS/Horus/DFM/RS41 sequential discovery, manual lock, local coordinate/history exports, Wi-Fi role controls and bounded SDR recovery.
- Generate installation-local hardware identities; exclude private station configuration, telemetry and credentials.
- Preserve upstream source/license notices. Weather uploader approval and generalized fresh-card validation remain separate work.
