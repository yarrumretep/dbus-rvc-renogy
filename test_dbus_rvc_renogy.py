#!/usr/bin/env python3
"""Offline tests using frames captured from a Renogy REGO bank."""

import importlib.util
from pathlib import Path
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

    def bind(self, address):
        self.bound = address

    def setblocking(self, blocking):
        self.blocking = blocking

    def fileno(self):
        return self.fd

    def recv(self, _size):
        if self.recv_error is not None:
            raise self.recv_error
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
