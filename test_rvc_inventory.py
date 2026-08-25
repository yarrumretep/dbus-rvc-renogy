#!/usr/bin/env python3
"""Offline tests for passive RV-C inventory diagnostics."""

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("rvc-inventory.py")
SPEC = importlib.util.spec_from_file_location("rvc_inventory", SCRIPT)
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


class CapturedBatteryDiagnosticsTests(unittest.TestCase):
    def decode(self, dgn, payload):
        _label, decoded, confidence = inventory.decode(
            dgn, bytes.fromhex(payload))
        self.assertEqual(confidence, "SPEC")
        return decoded

    def test_yellow_diagnostics_use_multi_instance_spn_format(self):
        pack_18 = self.decode(0x1FECA, "1446031280ffffff")
        pack_16 = self.decode(0x1FECA, "1446031080ffffff")

        self.assertIn("lamps=yellow:on,red:off", pack_18)
        self.assertIn(
            "SPN=28 (unlisted Battery SPN) inst=18", pack_18)
        self.assertIn(
            "SPN=28 (unlisted Battery SPN) inst=16", pack_16)
        self.assertIn("FMI=0 (above normal)", pack_18)
        self.assertNotIn("266755", pack_18)
        self.assertNotIn("266243", pack_16)

    def test_clear_diagnostic_reports_no_fault(self):
        decoded = self.decode(0x1FECA, "0546ffffffffffff")

        self.assertIn("lamps=yellow:off,red:off", decoded)
        self.assertIn("no active fault", decoded)

    def test_full_pack_has_charge_path_open(self):
        decoded = self.decode(0x1FE8B, "1201018e010000ff")

        self.assertIn("inst=18", decoded)
        self.assertIn("discharge=connected", decoded)
        self.assertIn("charge=disconnected", decoded)
        self.assertIn("full_cap=398 Ah", decoded)
        self.assertIn("P=0 W", decoded)

    def test_lower_pack_is_connected_and_accepting_charge(self):
        decoded = self.decode(0x1FE8B, "1101158e01f701ff")

        self.assertIn("inst=17", decoded)
        self.assertIn("discharge=connected", decoded)
        self.assertIn("charge=connected", decoded)
        self.assertIn("charge-detected=yes", decoded)
        self.assertIn("P=503 W", decoded)

    def test_standard_limit_flags_are_clear(self):
        decoded = self.decode(0x1FE90, "1201000000ffffff")

        self.assertIn("status=normal", decoded)
        self.assertIn("raw=000000", decoded)

    def test_standard_limit_and_disconnect_flags_are_named(self):
        decoded = self.decode(0x1FE90, "1201050000ffffff")

        self.assertIn("high-V-limit=yes", decoded)
        self.assertIn("high-V-disconnect=disconnected", decoded)

    def test_rvc_special_numeric_values_are_reported_as_unavailable(self):
        status_1 = self.decode(0x1FE95, "1278fefffeffffff")
        status_2 = self.decode(0x1FE94, "1278fefffefeffff")
        status_4 = self.decode(0x1FE92, "1278fdfefffefffd")

        self.assertIn("V=n/a", status_1)
        self.assertIn("I=n/a", status_1)
        self.assertIn("T=n/a", status_2)
        self.assertIn("SOC=n/a", status_2)
        self.assertIn("t_rem=n/a", status_2)
        self.assertIn("state=n/a", status_4)
        self.assertIn("CVL=n/a", status_4)
        self.assertIn("CCL=n/a", status_4)
        self.assertIn("type=n/a", status_4)

    def test_battery_summary_and_voltage_history(self):
        summary = self.decode(0x1FDF1, "1201010104ffffff")
        history = self.decode(0x1FDF2, "1201f0002201ffff")

        self.assertIn("modules=1", summary)
        self.assertIn("cells/module=4", summary)
        self.assertIn("V-status=n/a", summary)
        self.assertIn("lowest=12.00 V", history)
        self.assertIn("highest=14.50 V", history)


if __name__ == "__main__":
    unittest.main()
