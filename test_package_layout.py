#!/usr/bin/env python3
"""Offline checks for the persistent Venus OS service package."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
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
            ROOT / "deploy.sh",
            ROOT / "install-service.sh",
            ROOT / "uninstall-service.sh",
            ROOT / "rvc-inventory.py",
            ROOT / "services/dbus-rvc-renogy/run",
            ROOT / "services/dbus-rvc-renogy/log/run",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(os.access(path, os.X_OK))

    def test_shell_scripts_parse(self):
        scripts = (
            ("sh", ROOT / "deploy.sh"),
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
        self.assertIn("RVC_RENOGY_SERVICE_ROOT:-/service", installer)
        self.assertIn("ACTIVE_SERVICE=$SERVICE_ROOT/dbus-rvc-renogy", installer)

    def test_installer_restarts_an_existing_service(self):
        installer = (ROOT / "install-service.sh").read_text(encoding="utf-8")
        self.assertIn('svc -d "$ACTIVE_SERVICE"', installer)
        self.assertIn('svc -u "$ACTIVE_SERVICE"', installer)

    def test_install_and_uninstall_preserve_existing_rc_local(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            package = temp / "data" / "dbus-rvc-renogy"
            service_root = temp / "service"
            log_root = temp / "log"
            rc_local = temp / "data" / "rc.local"
            fake_bin = temp / "bin"

            package.mkdir(parents=True)
            service_root.mkdir()
            log_root.mkdir()
            fake_bin.mkdir()

            shutil.copy2(SCRIPT, package / SCRIPT.name)
            shutil.copy2(ROOT / "install-service.sh", package)
            shutil.copytree(ROOT / "services", package / "services")

            rc_local.write_text(
                "#!/bin/bash\n"
                "if [ ! -f /data/.ready ]; then\n"
                "    exit 0\n"
                "fi\n"
                "bash /data/etc/dbus-serialbattery/reinstall-local.sh\n",
                encoding="utf-8")

            fake_svc = fake_bin / "svc"
            fake_svc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_svc.chmod(0o755)

            env = os.environ.copy()
            env.update({
                "PATH": "%s:%s" % (fake_bin, env["PATH"]),
                "RVC_RENOGY_PACKAGE_DIR": str(package),
                "RVC_RENOGY_SERVICE_ROOT": str(service_root),
                "RVC_RENOGY_LOG_ROOT": str(log_root),
                "RVC_RENOGY_RC_LOCAL": str(rc_local),
            })

            for _ in range(2):
                subprocess.run(
                    ["sh", str(ROOT / "install-service.sh")],
                    check=True, env=env, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)

            contents = rc_local.read_text(encoding="utf-8")
            hook = "%s/install-service.sh --boot" % package
            existing = "bash /data/etc/dbus-serialbattery/reinstall-local.sh"
            self.assertIn(existing, contents)
            self.assertLess(contents.index("fi\n"), contents.index(hook))
            self.assertLess(contents.index(existing), contents.index(hook))
            self.assertEqual(contents.count(hook), 1)
            self.assertEqual(
                os.readlink(service_root / "dbus-rvc-renogy"),
                str(package / "services" / "dbus-rvc-renogy"))

            # Simulate Venus rebuilding /service during reboot, then execute
            # the command installed in the late-boot hook.
            (service_root / "dbus-rvc-renogy").unlink()
            subprocess.run(
                ["sh", str(ROOT / "install-service.sh"), "--boot"],
                check=True, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            self.assertTrue((service_root / "dbus-rvc-renogy").is_symlink())
            self.assertEqual(
                rc_local.read_text(encoding="utf-8").count(hook), 1)

            subprocess.run(
                ["sh", str(ROOT / "uninstall-service.sh")],
                check=True, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)

            contents = rc_local.read_text(encoding="utf-8")
            self.assertIn(existing, contents)
            self.assertNotIn("dbus-rvc-renogy", contents)
            self.assertFalse((service_root / "dbus-rvc-renogy").exists())


if __name__ == "__main__":
    unittest.main()
