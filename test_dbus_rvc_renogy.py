#!/usr/bin/env python3
"""Offline tests using frames captured from a Renogy REGO bank."""

import importlib.util
from pathlib import Path
import struct
import unittest


SCRIPT = Path(__file__).with_name("dbus-rvc-renogy.py")
SPEC = importlib.util.spec_from_file_location("dbus_rvc_renogy", SCRIPT)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RawValueDecoderTests(unittest.TestCase):
    def test_u8_rejects_all_rvc_special_values(self):
        self.assertEqual(bridge._u8(bytes([0xFC]), 0), 0xFC)
        for value in (0xFD, 0xFE, 0xFF):
            with self.subTest(value=value):
                self.assertIsNone(bridge._u8(bytes([value]), 0))

    def test_u16_rejects_all_rvc_special_values(self):
        self.assertEqual(
            bridge._u16(struct.pack("<H", 0xFFFC), 0), 0xFFFC)
        for value in (0xFFFD, 0xFFFE, 0xFFFF):
            with self.subTest(value=value):
                self.assertIsNone(
                    bridge._u16(struct.pack("<H", value), 0))

    def test_u32_rejects_all_rvc_special_values(self):
        self.assertEqual(
            bridge._u32(struct.pack("<I", 0xFFFFFFFC), 0), 0xFFFFFFFC)
        for value in (0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF):
            with self.subTest(value=value):
                self.assertIsNone(
                    bridge._u32(struct.pack("<I", value), 0))


class BatteryStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.state = bridge.BatteryState(clock=self.clock)

    def feed(self, dgn, payload):
        self.assertTrue(self.state.update(dgn, bytes.fromhex(payload)))

    def feed_complete_capture(self):
        self.feed(bridge.DGN_DC_SOURCE_STATUS_1, "01780a0120013577")
        self.feed(bridge.DGN_DC_SOURCE_STATUS_2, "01783c25aef40001")
        self.feed(bridge.DGN_DC_SOURCE_STATUS_3, "0178c81404aeffff")
        self.feed(bridge.DGN_DC_SOURCE_STATUS_4, "0178072001709403")
        self.feed(bridge.DGN_DC_SOURCE_STATUS_6, "0178000000ffffff")
        self.feed(bridge.DGN_DC_SOURCE_STATUS_11, "017815b004f40100")

    def test_capture_reproduces_native_service_values(self):
        self.feed_complete_capture()
        values = self.state.snapshot()

        self.assertEqual(values["/Connected"], 1)
        self.assertAlmostEqual(values["/Dc/0/Voltage"], 13.3)
        # Captured RV-C current is -37.6 A; Venus must report charging-positive.
        self.assertEqual(values["/Dc/0/Current"], 37.6)
        self.assertEqual(values["/Dc/0/Power"], 500)
        self.assertAlmostEqual(values["/Dc/0/Temperature"], 24.875)
        self.assertEqual(values["/Soc"], 87.0)
        self.assertEqual(values["/Soh"], 100.0)
        self.assertEqual(values["/Info/MaxChargeVoltage"], 14.4)
        self.assertEqual(values["/Info/MaxChargeCurrent"], 300.0)
        self.assertEqual(values["/Capacity"], 1200)

    def test_remaining_capacity_is_not_published_as_capacity(self):
        self.feed(bridge.DGN_DC_SOURCE_STATUS_1, "01780a0120013577")
        self.feed(bridge.DGN_DC_SOURCE_STATUS_3, "0178c81404aeffff")
        values = self.state.snapshot()

        self.assertEqual(self.state.remaining_capacity, 1044)
        self.assertNotIn("/Capacity", values)

        self.feed(bridge.DGN_DC_SOURCE_STATUS_11, "017815b004f40100")
        self.assertEqual(self.state.snapshot()["/Capacity"], 1200)

    def test_measurement_timeout_disconnects_and_stops_charge(self):
        self.feed_complete_capture()
        self.clock.now = bridge.MEASUREMENTS_STALE_AFTER + 0.1
        values = self.state.snapshot()

        self.assertEqual(values["/Connected"], 0)
        self.assertEqual(values["/Info/MaxChargeCurrent"], 0.0)

    def test_limit_timeout_stops_charge_while_measurements_remain_live(self):
        self.feed_complete_capture()
        self.clock.now = bridge.LIMITS_STALE_AFTER + 0.1
        self.feed(bridge.DGN_DC_SOURCE_STATUS_1, "01780a0120013577")
        values = self.state.snapshot()

        self.assertEqual(values["/Connected"], 1)
        self.assertEqual(values["/Info/MaxChargeCurrent"], 0.0)

    def test_implausible_voltage_limit_stops_charge(self):
        self.feed(bridge.DGN_DC_SOURCE_STATUS_1, "01780a0120013577")
        # CVL raw 0x0000 = 0 V; CCL remains 300 A.
        self.feed(bridge.DGN_DC_SOURCE_STATUS_4, "0178070000709403")
        values = self.state.snapshot()

        self.assertEqual(values["/Info/MaxChargeCurrent"], 0.0)
        self.assertNotIn("/Info/MaxChargeVoltage", values)

    def test_out_of_range_charge_current_limit_stops_charge(self):
        self.feed(bridge.DGN_DC_SOURCE_STATUS_1, "01780a0120013577")
        # CCL raw 0xFFFE means Error/Out of Range, not 1676.7 A.
        self.feed(bridge.DGN_DC_SOURCE_STATUS_4, "0178072001feff03")
        values = self.state.snapshot()

        self.assertIsNone(self.state.max_charge_current)
        self.assertEqual(values["/Info/MaxChargeCurrent"], 0.0)
        self.assertNotIn("/Info/MaxChargeVoltage", values)

    def test_out_of_range_voltage_disconnects_measurements(self):
        # Voltage raw 0xFFFE means Error/Out of Range, not 3276.7 V.
        self.feed(bridge.DGN_DC_SOURCE_STATUS_1, "0178feff20013577")
        self.feed(bridge.DGN_DC_SOURCE_STATUS_4, "0178072001709403")
        values = self.state.snapshot()

        self.assertIsNone(self.state.voltage)
        self.assertEqual(values["/Connected"], 0)
        self.assertEqual(values["/Info/MaxChargeCurrent"], 0.0)
        self.assertNotIn("/Dc/0/Voltage", values)

    def test_out_of_range_soc_is_not_published(self):
        self.feed(bridge.DGN_DC_SOURCE_STATUS_1, "01780a0120013577")
        # SOC raw 0xFE means Error/Out of Range, not 127 percent.
        self.feed(bridge.DGN_DC_SOURCE_STATUS_2, "01783c25fef40001")
        values = self.state.snapshot()

        self.assertIsNone(self.state.soc)
        self.assertNotIn("/Soc", values)

    def test_valid_limits_are_clamped_only_at_hard_ceilings(self):
        self.feed(bridge.DGN_DC_SOURCE_STATUS_1, "01780a0120013577")
        # CVL 14.8 V and CCL 400 A are valid encodings but exceed the bridge's
        # configured hard ceilings of 14.6 V and 300 A.
        self.feed(bridge.DGN_DC_SOURCE_STATUS_4, "0178072801409c03")
        values = self.state.snapshot()

        self.assertAlmostEqual(values["/Info/MaxChargeVoltage"], 14.6)
        self.assertAlmostEqual(values["/Info/MaxChargeCurrent"], 300.0)

    def test_wrong_instance_and_short_frames_are_ignored(self):
        self.assertFalse(self.state.update(
            bridge.DGN_DC_SOURCE_STATUS_1, bytes.fromhex("02780a0120013577")))
        self.assertFalse(self.state.update(
            bridge.DGN_DC_SOURCE_STATUS_1, bytes.fromhex("01780a01")))
        self.assertFalse(self.state.update(0x12345, bytes(8)))

    def test_service_readiness_requires_live_measurements_and_valid_limits(self):
        self.assertFalse(self.state.ready_for_service())

        self.feed(bridge.DGN_DC_SOURCE_STATUS_1, "01780a0120013577")
        self.assertFalse(self.state.ready_for_service())

        self.feed(bridge.DGN_DC_SOURCE_STATUS_4, "0178072001709403")
        self.assertTrue(self.state.ready_for_service())

        self.clock.now = bridge.LIMITS_STALE_AFTER + 0.1
        self.assertFalse(self.state.ready_for_service())


class FakeService:
    def __init__(self, name, register=False):
        self.name = name
        self.register_requested = register
        self.paths = {}
        self.registered = False

    def add_path(self, path, value):
        self.paths[path] = value

    def register(self):
        self.registered = True

    def __setitem__(self, path, value):
        self.paths[path] = value


class FakeSocket:
    next_fd = 10

    def __init__(self, *_args):
        self.fd = FakeSocket.next_fd
        FakeSocket.next_fd += 1
        self.bound = None
        self.blocking = None
        self.closed = False
        self.recv_error = None
        self.frames = []

    def bind(self, address):
        self.bound = address

    def setblocking(self, blocking):
        self.blocking = blocking

    def fileno(self):
        return self.fd

    def recv(self, _size):
        if self.recv_error is not None:
            raise self.recv_error
        if self.frames:
            return self.frames.pop(0)
        raise BlockingIOError()

    def close(self):
        self.closed = True


class FakeGLib:
    IO_IN = 1

    def __init__(self):
        self.watches = {}
        self.removed = []
        self.timeout = None

    def io_add_watch(self, fd, _condition, callback):
        self.watches[fd] = callback
        return fd

    def source_remove(self, watch):
        self.removed.append(watch)
        self.watches.pop(watch, None)

    def timeout_add(self, interval, callback):
        self.timeout = (interval, callback)


class ControllerStartupTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.glib = FakeGLib()
        self.sockets = []

        def socket_factory(*args):
            sock = FakeSocket(*args)
            self.sockets.append(sock)
            return sock

        self.controller = bridge.RvcBattery(
            self.glib, FakeService, socket_factory=socket_factory,
            clock=self.clock)

    def feed_frame(self, source_address, dgn, payload):
        data = bytes.fromhex(payload)
        can_id = (bridge.CAN_EFF_FLAG | (dgn << 8) | source_address)
        frame = struct.pack("<IB3x8s", can_id, len(data), data)
        self.controller._sock.frames.append(frame)
        self.assertTrue(self.controller._on_can_frame(
            self.controller._sock.fd, self.glib.IO_IN))

    def feed_aggregate(self, source_address, status_1="01780a0120013577"):
        self.feed_frame(
            source_address, bridge.DGN_DC_SOURCE_STATUS_1, status_1)
        self.feed_frame(
            source_address, bridge.DGN_DC_SOURCE_STATUS_4,
            "0178072001709403")
        self.feed_frame(
            source_address, bridge.DGN_DC_SOURCE_STATUS_11,
            "017815b004f40100")

    def test_automatic_discovery_accepts_aggregate_after_address_change(self):
        self.feed_aggregate(0x8E)

        self.assertEqual(self.controller._aggregator_sa, 0x8E)
        self.assertIsNotNone(self.controller._service)
        self.assertEqual(self.controller._service.paths["/Connected"], 1)
        self.assertEqual(
            self.controller._service.paths["/Info/MaxChargeVoltage"], 14.4)

    def test_automatic_discovery_accepts_address_above_observed_window(self):
        self.feed_aggregate(0x90)

        self.assertEqual(self.controller._aggregator_sa, 0x90)
        self.assertIsNotNone(self.controller._service)

    def test_automatic_discovery_accepts_address_below_observed_window(self):
        self.feed_aggregate(0x7F)

        self.assertEqual(self.controller._aggregator_sa, 0x7F)
        self.assertIsNotNone(self.controller._service)

    def test_automatic_discovery_ignores_gx_dc_source_rebroadcast(self):
        self.feed_aggregate(0xA1, status_1="01770a0120013577")

        self.assertIsNone(self.controller._aggregator_sa)
        self.assertIsNone(self.controller._service)

    def test_automatic_discovery_requires_battery_soc_priority(self):
        self.feed_aggregate(0x8E, status_1="01790a0120013577")

        self.assertIsNone(self.controller._aggregator_sa)
        self.assertIsNone(self.controller._service)

    def test_automatic_discovery_requires_bank_capacity_frame(self):
        self.feed_frame(
            0x8E, bridge.DGN_DC_SOURCE_STATUS_1, "01780a0120013577")
        self.feed_frame(
            0x8E, bridge.DGN_DC_SOURCE_STATUS_4, "0178072001709403")

        self.assertIsNone(self.controller._aggregator_sa)
        self.assertIsNone(self.controller._service)

    def test_automatic_discovery_follows_silent_aggregate_role(self):
        self.feed_aggregate(0x8D)
        self.assertEqual(self.controller._aggregator_sa, 0x8D)

        self.clock.now = bridge.SOURCE_SWITCH_AFTER + 0.1
        self.feed_aggregate(0x8E, status_1="017808010cdd3577")

        self.assertEqual(self.controller._aggregator_sa, 0x8E)
        self.assertAlmostEqual(
            self.controller._service.paths["/Dc/0/Voltage"], 13.2)

    def test_automatic_discovery_does_not_leave_a_live_aggregate(self):
        self.feed_aggregate(0x8D)
        self.feed_aggregate(0x8E, status_1="017808010cdd3577")

        self.assertEqual(self.controller._aggregator_sa, 0x8D)
        self.assertAlmostEqual(
            self.controller._service.paths["/Dc/0/Voltage"], 13.3)

    def test_service_is_withheld_until_live_limits_are_available(self):
        self.assertIsNone(self.controller._service)

        self.controller._state.update(
            bridge.DGN_DC_SOURCE_STATUS_1,
            bytes.fromhex("01780a0120013577"))
        self.controller._tick()
        self.assertIsNone(self.controller._service)

        self.controller._state.update(
            bridge.DGN_DC_SOURCE_STATUS_4,
            bytes.fromhex("0178072001709403"))
        self.controller._tick()

        service = self.controller._service
        self.assertIsNotNone(service)
        self.assertTrue(service.registered)
        self.assertEqual(service.paths["/Connected"], 1)
        self.assertEqual(service.paths["/Info/MaxChargeCurrent"], 300.0)
        self.assertEqual(service.paths["/Info/MaxChargeVoltage"], 14.4)

    def test_can_socket_is_rebound_when_measurements_never_arrive(self):
        first_socket = self.sockets[0]
        self.clock.now = bridge.CAN_REBIND_AFTER + 0.1
        self.controller._tick()

        self.assertEqual(len(self.sockets), 2)
        self.assertTrue(first_socket.closed)
        self.assertIn(first_socket.fd, self.glib.removed)
        self.assertIsNone(self.controller._service)

    def test_network_down_receive_error_is_retried_without_registering_bms(self):
        first_socket = self.sockets[0]
        first_socket.recv_error = OSError(100, "Network is down")

        keep_watch = self.controller._on_can_frame(first_socket.fd, 0)

        self.assertFalse(keep_watch)
        self.assertTrue(first_socket.closed)
        self.assertIsNone(self.controller._sock)
        self.assertIsNone(self.controller._service)

        self.clock.now = bridge.CAN_OPEN_RETRY_AFTER + 0.1
        self.controller._tick()

        self.assertEqual(len(self.sockets), 2)
        self.assertIs(self.controller._sock, self.sockets[1])
        self.assertIsNone(self.controller._service)


class ServiceContractTests(unittest.TestCase):
    def test_service_matches_the_working_venus_332_contract(self):
        service = bridge.RvcBattery._create_service(FakeService)

        self.assertEqual(service.name, bridge.SERVICE_NAME)
        self.assertTrue(service.registered)
        self.assertEqual(service.paths["/DeviceInstance"], 1)
        self.assertEqual(service.paths["/ProductId"], 0xB007)
        self.assertEqual(
            service.paths["/ProductName"], "CAN-bus BMS battery")
        self.assertEqual(service.paths["/Mgmt/Connection"], "RV-C")
        self.assertEqual(service.paths["/Info/MaxChargeCurrent"], 0.0)

        expected_alarms = {
            "/Alarms/HighCurrent",
            "/Alarms/HighTemperature",
            "/Alarms/HighVoltage",
            "/Alarms/LowSoc",
            "/Alarms/LowTemperature",
            "/Alarms/LowVoltage",
        }
        self.assertEqual(
            {path for path in service.paths if path.startswith("/Alarms/")},
            expected_alarms)


if __name__ == "__main__":
    unittest.main()
