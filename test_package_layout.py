#!/usr/bin/env python3
"""Offline checks for the persistent Venus OS service package."""

import importlib.util
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "dbus-rvc-renogy.py"
SPEC = importlib.util.spec_from_file_location("dbus_rvc_renogy", SCRIPT)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class PackageLayoutTests(unittest.TestCase):
    def test_package_version_matches_process_version(self):
        self.assertEqual(
            (ROOT / "version").read_text(encoding="utf-8").strip(),
            "v%s" % bridge.BRIDGE_VERSION)

    def test_entry_points_are_executable(self):
        paths = (
            ROOT / "install-service.sh",
            ROOT / "uninstall-service.sh",
            ROOT / "services/dbus-rvc-renogy/run",
            ROOT / "services/dbus-rvc-renogy/log/run",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(os.access(path, os.X_OK))

    def test_shell_scripts_parse(self):
        scripts = (
            ("sh", ROOT / "install-service.sh"),
            ("sh", ROOT / "uninstall-service.sh"),
            ("sh", ROOT / "services/dbus-rvc-renogy/run"),
            ("sh", ROOT / "services/dbus-rvc-renogy/log/run"),
        )
        for shell, script in scripts:
            with self.subTest(script=script):
                subprocess.run(
                    [shell, "-n", str(script)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_service_starts_the_persistent_bridge_path(self):
        run_script = (ROOT / "services/dbus-rvc-renogy/run").read_text(
            encoding="utf-8")
        self.assertIn(
            "/data/dbus-rvc-renogy/dbus-rvc-renogy.py", run_script)

    def test_service_name_matches_installer(self):
        self.assertTrue((ROOT / "services/dbus-rvc-renogy").is_dir())
        installer = (ROOT / "install-service.sh").read_text(encoding="utf-8")
        self.assertIn("/service/dbus-rvc-renogy", installer)

    def test_installer_restarts_an_existing_service(self):
        installer = (ROOT / "install-service.sh").read_text(encoding="utf-8")
        self.assertIn('svc -d "$ACTIVE_SERVICE"', installer)
        self.assertIn('svc -u "$ACTIVE_SERVICE"', installer)


if __name__ == "__main__":
    unittest.main()
