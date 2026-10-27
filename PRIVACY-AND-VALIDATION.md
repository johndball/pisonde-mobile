# Public-release privacy and validation notes

Package: **0.3.17**, prepared 27 October 2026.

## Privacy changes

- Removed installation-specific Wi-Fi MACs, AP UUID, hostname, operator callsign and saved SSID examples from distributed code and tests.
- Hardware identities are discovered during first-time setup and written only on the installing Pi. Source code no longer contains the original adapter identities.
- Uploads default to disabled and N0CALL; the placeholder cannot be enabled. Unit tests use synthetic station identities with mocked network operations.
- Removed the original flight serial/comparison narrative from the embedded guide. Retained only generic descriptions.
- Excluded configured NetworkManager profiles, hardware/settings JSON, credentials, SSH material, reception databases, CSV/GPX/JSONL flight exports, runtime logs, backups, private recordings, screenshots and remote-management software.
- Public default SSID/subnet, default receiving frequencies, upstream project URLs, GPL author attribution, synthetic test MACs and reserved fixture UUIDs remain intentionally.

The dashboard still displays the installing Pi's MAC addresses locally. This is useful for field troubleshooting; it does not mean those addresses were included in this download or sent as receiver positions to SondeHub. A configured device can contain private information and is not a redistribution source.

## Checks performed

- **135 offline regression tests passed**, covering receiver/scanner behavior, field UI, upload logic, Wi-Fi binding/forgetting, SDR recovery targeting, public hardware validation, AP activation rollback, setup identity generation and disabled-upload defaults.
- Python compilation and Bash syntax checks passed.
- Fresh package scanned for the original identifiers and common credential/key formats; only intended synthetic adapter/UUID values remain. Archive contents checked for runtime data and private artifacts.
- Source and documentation checksums are included in SHA256SUMS.

## Validation limits

The existing 0.3.10 receiver code has been exercised on the original field Pi. The new **public fresh-install workflow has not been run end-to-end on a Pi or a blank microSD**. Offline mocks do not verify Wi-Fi driver compatibility, USB power quality, decoder compilation on that hardware, phone rendering or live radio reception. Use a spare card and retain the known-working installation.

An intermittent SDR stall was mitigated by detection/recovery; its hardware cause has not been conclusively identified. This package does not promise arbitrary power-loss safety. SondeHub weather uploader approval remains external and uncompleted. APRS-IS socket writes do not establish downstream map ingestion.

The release-signing public key and its fingerprint are intentionally public. Private signing keys/passwords are never included. Signed-channel publication requires a publisher signature; device enrollment and RF validation remain device-side checks.

Retention remains disabled by default. Enabled cleanup deletes eligible active database receptions and linked upload/validation payloads; it does not securely erase SD blocks or purge existing backups, notes or diagnostic logs. Power controls use the existing local-dashboard/CSRF model and a narrow root broker. Keep the dashboard on a trusted network.
