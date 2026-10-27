"""Restrict public software packages to explicitly reviewed source files."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parent
# Adding a release file requires an explicit review of this allowlist.
ALLOWED={
    'docs/screenshots/README.md',
    'docs/screenshots/receiver.png',
    'docs/screenshots/updates.png',
    'docs/screenshots/wifi.png',
    'docs/screenshots/history.png',
    'docs/screenshots/uploads.png',
    'docs/screenshots/help.png',
    'docs/screenshots/weather-validation.png',

    'CHANGELOG.md',
    '.github/workflows/releases.yml',
    'tools/publish_releases.py',
    'releases/versions.json',
    'release-notes/v0.3.10.md',
    'release-notes/v0.3.11.md',
    'release-notes/v0.3.12.md',
    'release-notes/v0.3.13.md',
    'release-notes/v0.3.14.md',
    'test_publish_releases.py',

    'UPDATES.md',
    'bootstrap-updates.sh',
    'update_manager.py',
    'update_ui.py',
    'test_update_manager.py',
    'test_signatures.py',
    'tools/sign_release.py',
    'SIGNED-UPDATES.md',
    'enroll-signed-updates.py',
    'updates/signing-public.pem',
    'release-notes/v0.3.15.md',
    'updates/stable.json',
    'updates/signed-stable.json',
    'updates/candidates/0.3.15.json',

    '.gitattributes',
    '.gitignore',
    'AGENTS.md',
    'HARDWARE.md',
    'INSTALL.md',
    'LICENSE',
    'NOTICE.md',
    'PRIVACY-AND-VALIDATION.md',
    'README.md',
    'SHA256SUMS',
    'configure.py',
    'field_features.py',
    'field_ui.py',
    'hardware.py',
    'install.sh',
    'mobile.py',
    'rate-survey/COPYING',
    'rate-survey/PROVENANCE.json',
    'rate-survey/convenience/convenience.c',
    'rate-survey/convenience/convenience.h',
    'rate-survey/include/rtl-sdr.h',
    'rate-survey/rtl_power.c',
    'requirements.txt',
    'run_tests.py',
    'sdr_health.py',
    'storage_maintenance.py',
    'systemd/pisonde-history-backup.service',
    'systemd/pisonde-history-backup.timer',
    'systemd/pisonde-mobile.service',
    'systemd/pisonde-wifi-control.service',
    'test_field_features.py',
    'test_field_ui.py',
    'test_public_setup.py',
    'test_release_contents.py',
    'test_scanner.py',
    'test_sdr_health.py',
    'test_wifi_forget.py',
    'test_wifi_mac.py',
    'verify_recovery.py',
    'wifi_control.py',
}

class ReleaseContentsTests(unittest.TestCase):
    def test_only_reviewed_software_files_are_included(self):
        excluded={'.git','__pycache__','venv','.venv','horusdemodlib','radiosonde_auto_rx'}
        actual=set()
        for path in ROOT.rglob('*'):
            relative=path.relative_to(ROOT)
            if any(part in excluded for part in relative.parts):continue
            if path.is_file():actual.add(relative.as_posix())
        self.assertEqual(actual-{'updates/stable.json','updates/candidates/0.3.15.json'},ALLOWED-{'updates/stable.json','updates/candidates/0.3.15.json'},'Review unexpected files before creating a software release')
