#!/usr/bin/env python3
"""Publish the Renogy REGO RV-C bank as a Venus OS managed battery.

Venus OS 3.32 (dbus-rv-c 1.06.00) accepted the REGO bank through a generic
RV-C battery fallback. Venus OS 3.65+ uses named Lithionics and Battle Born
profiles instead, so dbus-rv-c 1.11.00 identifies the Renogy nodes but does
not create a ``com.victronenergy.battery`` service for them.

This read-only CAN bridge restores the small D-Bus contract observed on
Venus OS 3.32. It consumes the standard bank-level messages transmitted by
the REGO aggregator at source address 0x8D:

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


BRIDGE_VERSION = "0.4.2"

CAN_INTERFACE = os.environ.get("RVC_RENOGY_CAN_INTERFACE", "can0")
AGGREGATOR_SA = int(
    os.environ.get("RVC_RENOGY_SOURCE_ADDRESS", "0x8D"), 0)
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

CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF

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
    return None if value == 0xFF else value


def _u16(data, offset):
    value = struct.unpack_from("<H", data, offset)[0]
    return None if value == 0xFFFF else value


def _u32(data, offset):
    value = struct.unpack_from("<I", data, offset)[0]
    return None if value == 0xFFFFFFFF else value


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
        self.temperature = None
        self.soc = None
        self.soh = None
        self.remaining_capacity = None
        self.full_capacity = None

        self.charge_state = None
        self.max_charge_voltage = None
        self.max_charge_current = None
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

    def snapshot(self, now=None):
        """Return the D-Bus values that should be published at this instant."""
        now = self._clock() if now is None else now
        connected = self.measurements_fresh(now)
        cvl, ccl = self._safe_limits(now)

        values = {
            "/Connected": 1 if connected else 0,
            "/Info/MaxChargeCurrent": ccl,
        }
        if cvl is not None:
            values["/Info/MaxChargeVoltage"] = cvl

        if connected:
            values["/Dc/0/Voltage"] = self.voltage
            if self.current is not None:
                # The encoded field has 0.001 A resolution. Rounding removes
                # binary floating-point artifacts such as 8.099999999860302.
                values["/Dc/0/Current"] = round(self.current, 3)
                values["/Dc/0/Power"] = int(round(
                    self.voltage * self.current))
            if self.temperature is not None:
                values["/Dc/0/Temperature"] = self.temperature
            if self.soc is not None:
                values["/Soc"] = self.soc
            if self.soh is not None:
                values["/Soh"] = self.soh
            # The native 3.32 driver published full installed capacity here,
            # not the remaining-capacity value from STATUS_3.
            if self.full_capacity is not None:
                values["/Capacity"] = self.full_capacity

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
    def __init__(self, glib, service_class):
        self._glib = glib
        self._state = BatteryState()
        self._service = self._create_service(service_class)
        self._open_can()
        self._glib.timeout_add(1000, self._publish)

    @staticmethod
    def _create_service(service_class):
        service = service_class(SERVICE_NAME, register=False)
        service.add_path("/Mgmt/ProcessName", "dbus-rvc-renogy")
        service.add_path("/Mgmt/ProcessVersion", BRIDGE_VERSION)
        service.add_path("/Mgmt/Connection", "RV-C")
        service.add_path("/DeviceInstance", DEVICE_INSTANCE)
        service.add_path("/ProductId", PRODUCT_ID)
        service.add_path("/ProductName", PRODUCT_NAME)
        service.add_path("/FirmwareVersion", None)
        service.add_path("/HardwareVersion", None)
        service.add_path("/Connected", 0)
        service.add_path("/Serial", None)

        for path in (
                "/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power",
                "/Dc/0/Temperature", "/Soc", "/Soh", "/Capacity",
                "/TimeToGo", "/Info/MaxChargeVoltage"):
            service.add_path(path, None)

        service.add_path("/Info/MaxChargeCurrent", 0.0)

        for alarm in (
                "HighCurrent", "HighTemperature", "HighVoltage", "LowSoc",
                "LowTemperature", "LowVoltage"):
            service.add_path("/Alarms/%s" % alarm, 0)

        service.register()
        return service

    def _open_can(self):
        self._sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self._sock.bind((CAN_INTERFACE,))
        self._sock.setblocking(False)
        self._glib.io_add_watch(
            self._sock.fileno(), self._glib.IO_IN, self._on_can_frame)

    def _on_can_frame(self, _fd, _condition):
        try:
            frame = self._sock.recv(16)
        except BlockingIOError:
            return True

        if len(frame) < 16:
            return True

        can_id, dlc = struct.unpack_from("<IB", frame, 0)
        if not (can_id & CAN_EFF_FLAG):
            return True

        can_id &= CAN_EFF_MASK
        source_address = can_id & 0xFF
        dgn = (can_id >> 8) & 0x1FFFF
        if source_address != AGGREGATOR_SA or dgn not in AGGREGATE_DGNS:
            return True

        data = frame[8:8 + dlc]
        self._state.update(dgn, data)
        return True

    def _publish(self):
        for path, value in self._state.snapshot().items():
            self._service[path] = value
        return True


def main():
    from gi.repository import GLib
    import dbus.mainloop.glib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    sys.path.insert(
        1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
    from vedbus import VeDbusService

    print("dbus-rvc-renogy %s" % BRIDGE_VERSION)
    RvcBattery(GLib, VeDbusService)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
