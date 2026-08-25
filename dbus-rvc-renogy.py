#!/usr/bin/env python3
"""Publish the Renogy REGO RV-C bank as a Venus OS managed battery.

Venus OS 3.32 (dbus-rv-c 1.06.00) accepted the REGO bank through a generic
RV-C battery fallback. Venus OS 3.65+ uses named Lithionics and Battle Born
profiles instead, so dbus-rv-c 1.11.00 identifies the Renogy nodes but does
not create a ``com.victronenergy.battery`` service for them.

This read-only CAN bridge restores the small D-Bus contract observed on
Venus OS 3.32. It consumes the standard bank-level messages transmitted by
the REGO aggregator. The physical battery acting as aggregator can change its
RV-C source address after a bank restart, so the bridge discovers the live
aggregate source instead of assuming the initially observed address 0x8D:

    0x1FFFD  DC_SOURCE_STATUS_1   voltage and current
    0x1FFFC  DC_SOURCE_STATUS_2   temperature and state of charge
    0x1FFFB  DC_SOURCE_STATUS_3   state of health
    0x1FEC9  DC_SOURCE_STATUS_4   charge voltage/current limits
    0x1FEC7  DC_SOURCE_STATUS_6   battery status flags (retained, not guessed)
    0x1FEA5  DC_SOURCE_STATUS_11  full installed capacity

The bridge intentionally does not combine the per-pack BATTERY_STATUS frames.
The aggregator is the bank BMS: its 0x1FEC9 limit was the source used by the
working 3.32 driver, and only two of the three physical nodes publish the
per-pack family in the observed topology.

Safety properties:

* Never transmits on CAN.
* Publishes zero charge current until a fresh, valid status-4 frame arrives.
* Publishes zero charge current when the aggregate measurement stream is stale.
* Rejects implausible charge-voltage limits and clamps only hard maxima.
* Uses the charging-positive current convention expected by Venus OS; RV-C's
  captured REGO current is negative while charging.
"""

import math
import os
import socket
import struct
import sys
import time


BRIDGE_VERSION = "0.4.9"

CAN_INTERFACE = os.environ.get("RVC_RENOGY_CAN_INTERFACE", "can0")


def _parse_source_address(raw_value):
    raw_value = raw_value.strip()
    if raw_value.lower() == "auto":
        return None
    try:
        value = int(raw_value, 0)
    except ValueError:
        try:
            value = int(raw_value, 16)
        except ValueError:
            raise ValueError(
                "source address must be auto, decimal, 0x-prefixed hex, "
                "or bare hex (for example 8D)") from None
    if value < 0 or value > 0xFF:
        raise ValueError("source address must be between 0x00 and 0xFF")
    return value


def _configured_source_address():
    raw_value = os.environ.get("RVC_RENOGY_SOURCE_ADDRESS", "auto")
    try:
        return _parse_source_address(raw_value)
    except ValueError as error:
        raise SystemExit(
            "Invalid RVC_RENOGY_SOURCE_ADDRESS=%r: %s"
            % (raw_value, error)) from None


CONFIGURED_AGGREGATOR_SA = _configured_source_address()
DC_SOURCE_INSTANCE = 1
DEVICE_INSTANCE = int(os.environ.get("RVC_RENOGY_DEVICE_INSTANCE", "1"), 0)

SERVICE_NAME = "com.victronenergy.battery.rvc_renogy_%s" % CAN_INTERFACE
PRODUCT_ID = 0xB007
PRODUCT_NAME = "CAN-bus BMS battery"

# Hard safety bounds. Valid lower CVLs are not raised; a BMS may intentionally
# request a low voltage. Values outside the plausible range stop charging.
CVL_MIN_VALID = 10.0
CVL_MAX_VALID = 16.0
CVL_CEILING = float(os.environ.get("RVC_RENOGY_CVL_CEILING", "14.6"))
CCL_CEILING = float(os.environ.get("RVC_RENOGY_CCL_CEILING", "300.0"))

LOW_VOLTAGE_ALARM = 12.0
LOW_SOC_ALARM = 15.0
LOW_TEMPERATURE_ALARM = 0.0
HIGH_TEMPERATURE_ALARM = 50.0

MEASUREMENTS_STALE_AFTER = 10.0
# The aggregate limit frame was observed every five seconds.
LIMITS_STALE_AFTER = 15.0
# Reopen a socket that has not received measurement traffic. During Venus OS
# startup can0 can be reconfigured after the service first binds to it.
CAN_REBIND_AFTER = 10.0
CAN_OPEN_RETRY_AFTER = 2.0
# The observed REGO bank aggregate identifies itself as the Battery SOC source.
# Victron's RV-C export uses priority 119, so this distinguishes the physical
# bank without relying on either device retaining a particular CAN address.
RENOGY_AGGREGATE_PRIORITY = 120
# STATUS_1 is observed at 2 Hz. If a complete candidate aggregate appears and
# the current source has been silent for this long, the aggregate role moved.
SOURCE_SWITCH_AFTER = 3.0

CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF
# Linux constants are provided explicitly as fallbacks so the controller can
# be exercised by offline tests on non-Linux development systems.
SOCKET_AF_CAN = getattr(socket, "AF_CAN", 29)
SOCKET_CAN_RAW = getattr(socket, "CAN_RAW", 1)

DGN_DC_SOURCE_STATUS_1 = 0x1FFFD
DGN_DC_SOURCE_STATUS_2 = 0x1FFFC
DGN_DC_SOURCE_STATUS_3 = 0x1FFFB
DGN_DC_SOURCE_STATUS_4 = 0x1FEC9
DGN_DC_SOURCE_STATUS_6 = 0x1FEC7
DGN_DC_SOURCE_STATUS_11 = 0x1FEA5

AGGREGATE_DGNS = frozenset((
    DGN_DC_SOURCE_STATUS_1,
    DGN_DC_SOURCE_STATUS_2,
    DGN_DC_SOURCE_STATUS_3,
    DGN_DC_SOURCE_STATUS_4,
    DGN_DC_SOURCE_STATUS_6,
    DGN_DC_SOURCE_STATUS_11,
))


def _u8(data, offset):
    value = data[offset]
    # RV-C reserves the top three values for Reserved, Error/Out of Range,
    # and Data Not Available. None keeps every caller from interpreting any
    # of those protocol sentinels as a measurement.
    return None if value >= 0xFD else value


def _u16(data, offset):
    value = struct.unpack_from("<H", data, offset)[0]
    return None if value >= 0xFFFD else value


def _u32(data, offset):
    value = struct.unpack_from("<I", data, offset)[0]
    return None if value >= 0xFFFFFFFD else value


def _volts(data, offset):
    value = _u16(data, offset)
    return None if value is None else value * 0.05


def _measured_amps(data, offset):
    value = _u32(data, offset)
    return None if value is None else (value * 0.001) - 2000000.0


def _limit_amps(data, offset):
    value = _u16(data, offset)
    return None if value is None else (value * 0.05) - 1600.0


def _percentage(data, offset):
    value = _u8(data, offset)
    return None if value is None else value * 0.5


def _temperature(data, offset):
    value = _u16(data, offset)
    return None if value is None else (value * 0.03125) - 273.0


class BatteryState:
    """Pure decoder and freshness policy, kept independent of D-Bus for tests."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic

        self.voltage = None
        self.current = None
        self.source_priority = None
        self.temperature = None
        self.soc = None
        self.soh = None
        self.remaining_capacity = None
        self.full_capacity = None

        self.charge_state = None
        self.max_charge_voltage = None
        self.max_charge_current = None
        self.last_safe_charge_voltage = None
        self.status_6_flags = None

        self.measurements_at = None
        self.limits_at = None

    def update(self, dgn, data):
        """Decode one eight-byte bank aggregate payload.

        Returns True when the frame belongs to DC source instance 1 and was
        consumed, otherwise False.
        """
        if dgn not in AGGREGATE_DGNS or len(data) < 8:
            return False
        if data[0] != DC_SOURCE_INSTANCE:
            return False

        now = self._clock()

        if dgn == DGN_DC_SOURCE_STATUS_1:
            self.source_priority = _u8(data, 1)
            self.voltage = _volts(data, 2)
            rvc_current = _measured_amps(data, 4)
            # The REGO/RV-C capture is negative while charging; Venus is
            # charging-positive. This reproduces the working 3.32 service.
            self.current = None if rvc_current is None else -rvc_current
            self.measurements_at = now

        elif dgn == DGN_DC_SOURCE_STATUS_2:
            self.temperature = _temperature(data, 2)
            self.soc = _percentage(data, 4)

        elif dgn == DGN_DC_SOURCE_STATUS_3:
            self.soh = _percentage(data, 2)
            self.remaining_capacity = _u16(data, 3)

        elif dgn == DGN_DC_SOURCE_STATUS_4:
            self.charge_state = _u8(data, 2)
            self.max_charge_voltage = _volts(data, 3)
            self.max_charge_current = _limit_amps(data, 5)
            self.limits_at = now

        elif dgn == DGN_DC_SOURCE_STATUS_6:
            # The exact bit mapping has not yet been corroborated against the
            # RV-C specification. Preserve it for diagnostics; do not invent
            # alarm semantics that can influence system behavior.
            self.status_6_flags = bytes(data[2:5])

        elif dgn == DGN_DC_SOURCE_STATUS_11:
            self.full_capacity = _u16(data, 3)

        return True

    def measurements_fresh(self, now=None):
        now = self._clock() if now is None else now
        return (self.voltage is not None
                and self.measurements_at is not None
                and (now - self.measurements_at) < MEASUREMENTS_STALE_AFTER)

    def limits_fresh(self, now=None):
        now = self._clock() if now is None else now
        return (self.max_charge_voltage is not None
                and self.max_charge_current is not None
                and self.limits_at is not None
                and (now - self.limits_at) < LIMITS_STALE_AFTER)

    def _safe_limits(self, now):
        if not self.measurements_fresh(now) or not self.limits_fresh(now):
            return None, 0.0

        cvl = self.max_charge_voltage
        ccl = self.max_charge_current
        if not (math.isfinite(cvl) and math.isfinite(ccl)):
            return None, 0.0
        if cvl < CVL_MIN_VALID or cvl > CVL_MAX_VALID:
            return None, 0.0

        return min(cvl, CVL_CEILING), min(max(ccl, 0.0), CCL_CEILING)

    def ready_for_service(self, now=None):
        """True only when registering a live BMS service is safe.

        Publishing a disconnected service during GX startup can make DVCC
        select it and immediately raise Lost BMS. Wait for both aggregate
        measurements and valid charge limits before appearing on D-Bus.
        """
        now = self._clock() if now is None else now
        cvl, _ccl = self._safe_limits(now)
        return cvl is not None

    def snapshot(self, now=None):
        """Return the D-Bus values that should be published at this instant."""
        now = self._clock() if now is None else now
        connected = self.measurements_fresh(now)
        cvl, ccl = self._safe_limits(now)
        if cvl is not None:
            self.last_safe_charge_voltage = cvl

        current = (round(self.current, 3)
                   if connected and self.current is not None else None)
        power = (int(round(self.voltage * self.current))
                 if connected and self.current is not None else None)

        values = {
            "/Connected": 1 if connected else 0,
            "/Info/MaxChargeCurrent": ccl,
            # Once registered, retaining the last validated CVL keeps Venus
            # from reclassifying this service as a lost BMS. CCL=0 remains
            # the fail-safe whenever current limits or measurements are stale.
            "/Info/MaxChargeVoltage": (
                cvl if cvl is not None else self.last_safe_charge_voltage),
            "/Dc/0/Voltage": self.voltage if connected else None,
            "/Dc/0/Current": current,
            "/Dc/0/Power": power,
            "/Dc/0/Temperature": self.temperature if connected else None,
            "/Soc": self.soc if connected else None,
            "/Soh": self.soh if connected else None,
            "/Capacity": self.full_capacity if connected else None,
        }

        voltage = self.voltage if connected else None
        temperature = self.temperature if connected else None
        soc = self.soc if connected else None
        current = self.current if connected else None

        values.update({
            "/Alarms/LowVoltage": (
                2 if voltage is not None and voltage < LOW_VOLTAGE_ALARM else 0),
            "/Alarms/HighVoltage": 0,
            "/Alarms/LowSoc": (
                1 if soc is not None and soc < LOW_SOC_ALARM else 0),
            "/Alarms/HighCurrent": (
                2 if current is not None and abs(current) > CCL_CEILING else 0),
            "/Alarms/LowTemperature": (
                2 if temperature is not None
                and temperature < LOW_TEMPERATURE_ALARM else 0),
            "/Alarms/HighTemperature": (
                2 if temperature is not None
                and temperature > HIGH_TEMPERATURE_ALARM else 0),
        })
        return values


class RvcBattery:
    def __init__(self, glib, service_class, socket_factory=None, clock=None,
                 aggregator_sa=CONFIGURED_AGGREGATOR_SA):
        self._glib = glib
        self._clock = clock or time.monotonic
        self._state = BatteryState(clock=self._clock)
        self._configured_aggregator_sa = aggregator_sa
        self._aggregator_sa = aggregator_sa
        self._candidate_states = {}
        self._service_class = service_class
        self._service = None
        self._socket_factory = socket_factory or socket.socket
        self._sock = None
        self._can_watch = None
        self._can_opened_at = None
        self._last_can_open_attempt = None
        self._fixed_priority_warning_printed = False
        self._open_can()
        self._glib.timeout_add(1000, self._tick)

    @staticmethod
    def _candidate_ready(state, now):
        # STATUS_11 capacity distinguishes the bank aggregator from charging
        # devices that may publish a subset of the DC source status family.
        # Priority 120 identifies the Battery SOC source and excludes the GX's
        # priority-119 RV-C rebroadcast without assuming fixed CAN addresses.
        return (state.source_priority == RENOGY_AGGREGATE_PRIORITY
                and state.full_capacity is not None
                and state.full_capacity > 0
                and state.ready_for_service(now))

    def _select_aggregate_source(self, source_address, state):
        changed = source_address != self._aggregator_sa
        self._aggregator_sa = source_address
        self._state = state
        if changed:
            print("Selected Renogy aggregate source 0x%02X"
                  % source_address, flush=True)

    def _consume_aggregate_frame(self, source_address, dgn, data):
        if self._configured_aggregator_sa is not None:
            if source_address != self._configured_aggregator_sa:
                return False
            if not self._state.update(dgn, data):
                return False
            if self._state.source_priority != RENOGY_AGGREGATE_PRIORITY:
                if (self._state.source_priority is not None
                        and not self._fixed_priority_warning_printed):
                    print(
                        "Ignoring configured source 0x%02X: DC source "
                        "priority %d is not Renogy aggregate priority %d"
                        % (source_address, self._state.source_priority,
                           RENOGY_AGGREGATE_PRIORITY),
                        flush=True)
                    self._fixed_priority_warning_printed = True
                return False
            return True

        # 254 is the J1939 null address and 255 is the global address; neither
        # can identify a claimed source node.
        if source_address >= 0xFE:
            return False

        state = self._candidate_states.get(source_address)
        if state is None:
            state = BatteryState(clock=self._clock)
            self._candidate_states[source_address] = state
        if not state.update(dgn, data):
            return False

        now = self._clock()
        if source_address == self._aggregator_sa:
            self._state = state
        elif self._candidate_ready(state, now):
            current_silent = (
                self._aggregator_sa is None
                or self._state.measurements_at is None
                or (now - self._state.measurements_at) >= SOURCE_SWITCH_AFTER)
            if current_silent:
                self._select_aggregate_source(source_address, state)

        return source_address == self._aggregator_sa

    @staticmethod
    def _create_service(service_class, initial_values=None):
        initial_values = initial_values or {}

        def initial(path, default=None):
            return initial_values.get(path, default)

        service = service_class(SERVICE_NAME, register=False)
        service.add_path("/Mgmt/ProcessName", "dbus-rvc-renogy")
        service.add_path("/Mgmt/ProcessVersion", BRIDGE_VERSION)
        service.add_path("/Mgmt/Connection", "RV-C")
        service.add_path("/DeviceInstance", DEVICE_INSTANCE)
        service.add_path("/ProductId", PRODUCT_ID)
        service.add_path("/ProductName", PRODUCT_NAME)
        service.add_path("/FirmwareVersion", None)
        service.add_path("/HardwareVersion", None)
        service.add_path("/Connected", initial("/Connected", 0))
        service.add_path("/Serial", None)

        for path in (
                "/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power",
                "/Dc/0/Temperature", "/Soc", "/Soh", "/Capacity",
                "/TimeToGo", "/Info/MaxChargeVoltage"):
            service.add_path(path, initial(path))

        service.add_path(
            "/Info/MaxChargeCurrent",
            initial("/Info/MaxChargeCurrent", 0.0))

        for alarm in (
                "HighCurrent", "HighTemperature", "HighVoltage", "LowSoc",
                "LowTemperature", "LowVoltage"):
            path = "/Alarms/%s" % alarm
            service.add_path(path, initial(path, 0))

        service.register()
        return service

    def _close_can(self):
        if self._can_watch is not None:
            self._glib.source_remove(self._can_watch)
            self._can_watch = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _open_can(self):
        self._close_can()
        now = self._clock()
        self._last_can_open_attempt = now
        sock = None
        try:
            sock = self._socket_factory(
                SOCKET_AF_CAN, socket.SOCK_RAW, SOCKET_CAN_RAW)
            sock.bind((CAN_INTERFACE,))
            sock.setblocking(False)
            watch = self._glib.io_add_watch(
                sock.fileno(), self._glib.IO_IN, self._on_can_frame)
        except OSError as error:
            try:
                sock.close()
            except (AttributeError, OSError):
                pass
            print("CAN open failed on %s: %s" % (CAN_INTERFACE, error),
                  flush=True)
            return False

        self._sock = sock
        self._can_watch = watch
        self._can_opened_at = now
        print("Listening for Renogy aggregate frames on %s" % CAN_INTERFACE,
              flush=True)
        return True

    def _drop_can_from_callback(self, error):
        print("CAN receive failed on %s: %s" % (CAN_INTERFACE, error),
              flush=True)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        # Returning False removes the current GLib watch.
        self._can_watch = None

    def _on_can_frame(self, _fd, _condition):
        try:
            frame = self._sock.recv(16)
        except BlockingIOError:
            return True
        except OSError as error:
            self._drop_can_from_callback(error)
            return False

        if len(frame) < 16:
            return True

        can_id, dlc = struct.unpack_from("<IB", frame, 0)
        if not (can_id & CAN_EFF_FLAG):
            return True

        can_id &= CAN_EFF_MASK
        source_address = can_id & 0xFF
        dgn = (can_id >> 8) & 0x1FFFF
        if dgn not in AGGREGATE_DGNS:
            return True

        data = frame[8:8 + dlc]
        if self._consume_aggregate_frame(source_address, dgn, data):
            self._ensure_service()
            if self._service is not None:
                self._publish()
        return True

    def _ensure_service(self, now=None):
        now = self._clock() if now is None else now
        if self._service is not None or not self._state.ready_for_service(now):
            return False

        initial_values = self._state.snapshot(now)
        self._service = self._create_service(
            self._service_class, initial_values=initial_values)
        print("Registered live BMS service %s" % SERVICE_NAME, flush=True)
        return True

    def _publish(self):
        if self._service is None:
            return
        for path, value in self._state.snapshot().items():
            self._service[path] = value

    def _tick(self):
        now = self._clock()

        if self._sock is None:
            if (self._last_can_open_attempt is None
                    or (now - self._last_can_open_attempt)
                    >= CAN_OPEN_RETRY_AFTER):
                self._open_can()
        elif (self._can_opened_at is not None
              and (now - self._can_opened_at) >= CAN_REBIND_AFTER
              and not self._state.measurements_fresh(now)):
            print("No fresh Renogy measurements; rebinding %s" % CAN_INTERFACE,
                  flush=True)
            self._open_can()

        self._ensure_service(now)
        self._publish()
        return True


def main():
    from gi.repository import GLib
    import dbus.mainloop.glib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    sys.path.insert(
        1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
    from vedbus import VeDbusService

    print("dbus-rvc-renogy %s" % BRIDGE_VERSION, flush=True)
    RvcBattery(GLib, VeDbusService)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
