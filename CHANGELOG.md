# Changelog

Application versions, stable-channel deployments and GitHub Releases are related but distinct. A device installs only the immutable source snapshot named in `updates/stable.json`; publishing a tag alone does not deploy code. GitHub Releases are available from the repository's Releases page.

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
