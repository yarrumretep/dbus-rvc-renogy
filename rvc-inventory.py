#!/usr/bin/env python3
"""
rvc-inventory — RV-C bus inventory with inline payload decoding.

Read-only. Never transmits. Safe alongside Victron's dbus-rvc.

    ./rvc-inventory.py                        # live table, refresh every 10 s
    ./rvc-inventory.py --sa 8D,8C,8E          # only these nodes
    ./rvc-inventory.py --watch-dm             # baseline DM_RV, report changes
    ./rvc-inventory.py --stream               # every frame, decoded, as it lands
    ./rvc-inventory.py --jitter 1FEC9         # inter-arrival times for one DGN

--- CONFIDENCE ------------------------------------------------------------
Decoders are tagged in the DGN table below:

  [SPEC]      corroborated by published vendor/RV-C documentation
  [DERIVED]   inferred from observed traffic, arithmetic self-consistent
              across independent fields, but NOT checked against the spec
  [GUESS]     partial; the fields shown are the ones that behave sensibly

The authoritative source is the RV-C spec itself, which is public at
rv-c.com. Check anything marked [DERIVED] or [GUESS] against it before you
let it drive a charge decision.

--- WHAT'S KNOWN ----------------------------------------------------------
DC_SOURCE_STATUS_4 is 0x1FEC9 and DC_SOURCE_STATUS_6 is 0x1FEC7 -- NOT
0x1FFFA/0x1FFF8. The series does not count down from 0x1FFFD. Confirmed by
Lithionics' published NeverDie BMS RV-C protocol document.

BATTERY_STATUS_1..11 occupy 0x1FE95 down to 0x1FE8B (eleven consecutive
DGNs). RV-C added them for smart Li-ion packs so each battery can be
monitored individually while DC_SOURCE_STATUS still carries the bank
aggregate. Their payloads mirror the corresponding DC_SOURCE_STATUS_n.
"""

import argparse
import socket
import struct
import sys
import time
from collections import defaultdict, deque

CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF

# PDU1 DGNs: the low byte is the destination address, not part of the DGN.
PDU1_BASES = {0x0EA00: "REQUEST", 0x0E800: "ACK", 0x0EE00: "ADDRESS_CLAIM"}

NA8, NA16, NA32 = 0xFF, 0xFFFF, 0xFFFFFFFF


def u8(d, i):
    return None if d[i] == NA8 else d[i]


def u16(d, i):
    v = struct.unpack_from("<H", d, i)[0]
    return None if v == NA16 else v


def u32(d, i):
    v = struct.unpack_from("<I", d, i)[0]
    return None if v == NA32 else v


def volts(d, i):
    v = u16(d, i)
    return None if v is None else v * 0.05


def amps_offset(d, i):
    """0.05 A/bit, offset -1600 A. Used by the 'desired current' fields."""
    v = u16(d, i)
    return None if v is None else (v * 0.05) - 1600


def amps_precise(d, i):
    """0.001 A/bit, offset -2,000,000 A. Used by measured DC current."""
    v = u32(d, i)
    return None if v is None else (v * 0.001) - 2000000


def pct_half(d, i):
    v = u8(d, i)
    return None if v is None else v * 0.5


def degc(d, i):
    v = u16(d, i)
    return None if v is None else (v * 0.03125) - 273


def fmt(label, value, unit="", places=2):
    if value is None:
        return "%s=n/a" % label
    if isinstance(value, float):
        return "%s=%.*f%s" % (label, places, value, unit)
    return "%s=%s%s" % (label, value, unit)


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------

def dec_status_1(d):
    """DC_SOURCE_STATUS_1 / BATTERY_STATUS_1 — voltage and current."""
    return "  ".join([
        fmt("inst", d[0]), fmt("pri", d[1]),
        fmt("V", volts(d, 2), " V"),
        fmt("I", amps_precise(d, 4), " A", 1),
    ])


def dec_status_2(d):
    """DC_SOURCE_STATUS_2 / BATTERY_STATUS_2 — temperature and SOC."""
    tr = u16(d, 5)
    return "  ".join([
        fmt("inst", d[0]),
        fmt("T", degc(d, 2), " C", 1),
        fmt("SOC", pct_half(d, 4), "%", 1),
        fmt("t_rem", tr, " min") if tr is not None else "t_rem=n/a",
    ])


def dec_status_3(d):
    """DC_SOURCE_STATUS_3 / BATTERY_STATUS_3 — health and capacity."""
    return "  ".join([
        fmt("inst", d[0]),
        fmt("SOH", pct_half(d, 2), "%", 1),
        fmt("cap", u16(d, 3), " Ah"),
        fmt("rel", pct_half(d, 5), "%", 1),
    ])


def dec_status_4(d):
    """DC_SOURCE_STATUS_4 / BATTERY_STATUS_4 — the charge limits DVCC needs."""
    return "  ".join([
        fmt("inst", d[0]),
        fmt("state", u8(d, 2)),
        fmt("CVL", volts(d, 3), " V"),
        fmt("CCL", amps_offset(d, 5), " A", 1),
        fmt("type", u8(d, 7)),
    ])


def dec_status_6(d):
    """DC_SOURCE_STATUS_6 — LVC/HVC alarms and disconnect status. [SPEC name,
    GUESS layout] Bit-pair fields; 3 = no data, 2 = error, 1 = yes, 0 = no."""
    pairs = []
    for byte_i in (2, 3, 4):
        if d[byte_i] == NA8:
            continue
        for shift in (0, 2, 4, 6):
            pairs.append((d[byte_i] >> shift) & 0x03)
    active = [i for i, v in enumerate(pairs) if v == 1]
    return "  ".join([
        fmt("inst", d[0]),
        "flags=%s" % (",".join("b%d" % i for i in active) if active else "none"),
        "raw=%s" % d[2:5].hex(),
    ])


def dec_status_11(d):
    """DC_SOURCE_STATUS_11 — full capacity and DC power. [DERIVED]
    Power cross-checks against V*I from STATUS_1 to within 1 W."""
    return "  ".join([
        fmt("inst", d[0]),
        fmt("full_cap", u16(d, 3), " Ah"),
        fmt("P", u16(d, 5), " W"),
        "b2=0x%02X" % d[2],
    ])


def dec_dm_rv(d):
    """DM_RV / DM01 — diagnostics. Non-0xFF fault bytes mean something is
    being reported. Spec: broadcast every 5 s idle, faster when active."""
    dsa = d[1]
    quiet = all(b == NA8 for b in d[2:6])
    if quiet:
        return "op=0x%02X  DSA=%d (%s)  no active fault" % (
            d[0], dsa, DSA_NAMES.get(dsa, "?"))
    spn = d[2] | (d[3] << 8) | ((d[4] >> 5) << 16)
    fmi = d[4] & 0x1F
    return "op=0x%02X  DSA=%d (%s)  *** SPN=%d FMI=%d occ=%d ***" % (
        d[0], dsa, DSA_NAMES.get(dsa, "?"), spn, fmi, d[5] & 0x7F)


def dec_date_time(d):
    """DATE_TIME_STATUS."""
    return "20%02d-%02d-%02d  dow=%d  %02d:%02d:%02d  tz=%d" % (
        d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7])


def dec_inverter_ac_1(d):
    """INVERTER_AC_STATUS_1. Voltage and frequency are solid; the current
    field is [GUESS] -- shown raw so you can work it out."""
    f = u16(d, 5)
    return "  ".join([
        "inst/line=0x%02X" % d[0],
        fmt("Vac", volts(d, 1), " V", 1),
        fmt("Hz", f * 0.0078125 if f is not None else None, " Hz"),
        "I_raw=%s" % d[3:5].hex(),
    ])


def dec_charger_status_2(d):
    """CHARGER_STATUS_2. [DERIVED] The current field reads exactly 0.0 A on an
    idle charger (raw 32000), which is what pins the -1600 offset."""
    return "  ".join([
        fmt("inst", d[0]),
        fmt("V", volts(d, 3), " V"),
        fmt("I", amps_offset(d, 5), " A", 1),
        "b1b2=%s" % d[1:3].hex(),
    ])


def dec_request(d):
    dgn = d[0] | (d[1] << 8) | (d[2] << 16)
    return "wants=%s  inst=0x%02X  bank=0x%02X" % (dgn_label(dgn), d[3], d[4])


ACK_CTRL = {0: "ACK", 1: "NACK", 2: "ACCESS DENIED", 3: "CANNOT RESPOND"}


def dec_ack(d):
    dgn = d[5] | (d[6] << 8) | (d[7] << 16)
    return "%s  to=0x%02X  re=%s" % (
        ACK_CTRL.get(d[0], "0x%02X" % d[0]), d[4], dgn_label(dgn))


def dec_address_claim(d):
    name = struct.unpack("<Q", d[:8])[0]
    fn = (name >> 40) & 0xFF
    return "mfg=%d  function=%d (%s)  fn_inst=%d  ecu_inst=%d  id=%d" % (
        (name >> 21) & 0x7FF, fn, DSA_NAMES.get(fn, "?"),
        (name >> 35) & 0x1F, (name >> 32) & 0x07, name & 0x1FFFFF)


# ---------------------------------------------------------------------------
# DGN registry:  dgn -> (name, decoder, confidence)
# ---------------------------------------------------------------------------
DGNS = {
    # --- DC_SOURCE: the bank aggregate ---
    0x1FFFD: ("DC_SOURCE_STATUS_1", dec_status_1, "SPEC"),
    0x1FFFC: ("DC_SOURCE_STATUS_2", dec_status_2, "SPEC"),
    0x1FFFB: ("DC_SOURCE_STATUS_3", dec_status_3, "SPEC"),
    0x1FEC9: ("DC_SOURCE_STATUS_4", dec_status_4, "SPEC"),
    0x1FEC7: ("DC_SOURCE_STATUS_6", dec_status_6, "GUESS"),
    0x1FEA5: ("DC_SOURCE_STATUS_11", dec_status_11, "DERIVED"),

    # --- BATTERY_STATUS_1..11: per-pack, 0x1FE95 down to 0x1FE8B ---
    0x1FE95: ("BATTERY_STATUS_1", dec_status_1, "DERIVED"),
    0x1FE94: ("BATTERY_STATUS_2", dec_status_2, "DERIVED"),
    0x1FE93: ("BATTERY_STATUS_3", dec_status_3, "DERIVED"),
    0x1FE92: ("BATTERY_STATUS_4", dec_status_4, "DERIVED"),
    0x1FE91: ("BATTERY_STATUS_5", None, "SPEC"),
    0x1FE90: ("BATTERY_STATUS_6", None, "SPEC"),
    0x1FE8F: ("BATTERY_STATUS_7", None, "SPEC"),
    0x1FE8E: ("BATTERY_STATUS_8", None, "SPEC"),
    0x1FE8D: ("BATTERY_STATUS_9", None, "SPEC"),
    0x1FE8C: ("BATTERY_STATUS_10", None, "SPEC"),
    0x1FE8B: ("BATTERY_STATUS_11", None, "SPEC"),
    0x1FE8A: ("BATTERY_COMMAND?", None, "GUESS"),

    # --- diagnostics ---
    0x1FECA: ("DM_RV", dec_dm_rv, "SPEC"),
    0x0FECA: ("DM01(J1939)", dec_dm_rv, "SPEC"),

    # --- inverter / charger ---
    0x1FFD4: ("INVERTER_STATUS", None, "SPEC"),
    0x1FFD7: ("INVERTER_AC_STATUS_1", dec_inverter_ac_1, "DERIVED"),
    0x1FFC7: ("CHARGER_STATUS", None, "SPEC"),
    0x1FFC9: ("CHARGER_AC_STATUS_2", None, "SPEC"),
    0x1FFCA: ("CHARGER_AC_STATUS_1", None, "SPEC"),
    0x1FEA3: ("CHARGER_STATUS_2", dec_charger_status_2, "DERIVED"),

    # --- misc ---
    0x1FFFF: ("DATE_TIME_STATUS", dec_date_time, "DERIVED"),
    0x0FEEB: ("PRODUCT_ID", None, "SPEC"),
    0x0FED5: ("(unknown 0x0FED5)", None, "GUESS"),
}

PDU1_DECODERS = {
    "REQUEST": dec_request,
    "ACK": dec_ack,
    "ADDRESS_CLAIM": dec_address_claim,
}

# Only the entries confirmed in documentation. Everything else stays "?".
DSA_NAMES = {
    66: "Inverter",
    68: "Control Panel (GX)",
    70: "Battery",
}


def dgn_label(dgn):
    if dgn in DGNS:
        return DGNS[dgn][0]
    base = dgn & 0x1FF00
    if base in PDU1_BASES:
        return "%s->%02X" % (PDU1_BASES[base], dgn & 0xFF)
    return "0x%05X" % dgn


def decode(dgn, data):
    """Returns (label, decoded_string_or_None, confidence)."""
    if len(data) < 8:
        base = dgn & 0x1FF00
        if base in PDU1_BASES and len(data) >= 3:
            pass
        else:
            return dgn_label(dgn), None, ""
    base = dgn & 0x1FF00
    if base in PDU1_BASES:
        name = PDU1_BASES[base]
        fn = PDU1_DECODERS.get(name)
        try:
            return dgn_label(dgn), (fn(data) if fn else None), "SPEC"
        except Exception:
            return dgn_label(dgn), None, "SPEC"
    if dgn in DGNS:
        name, fn, conf = DGNS[dgn]
        if fn is None or len(data) < 8:
            return name, None, conf
        try:
            return name, fn(data), conf
        except Exception as e:
            return name, "decode error: %s" % e, conf
    return dgn_label(dgn), None, ""


# ---------------------------------------------------------------------------

class Node:
    def __init__(self, sa):
        self.sa = sa
        self.dgns = defaultdict(lambda: {"n": 0, "last": b"", "t0": None,
                                         "t1": None, "gaps": deque(maxlen=64)})
        self.dsa = None
        self.fault = False

    def observe(self, dgn, data, t):
        e = self.dgns[dgn]
        if e["t1"] is not None:
            e["gaps"].append(t - e["t1"])
        e["n"] += 1
        e["last"] = data
        e["t1"] = t
        if e["t0"] is None:
            e["t0"] = t
        if dgn in (0x1FECA, 0x0FECA) and len(data) >= 6:
            self.dsa = data[1]
            self.fault = not all(b == NA8 for b in data[2:6])

    def rate(self, dgn):
        e = self.dgns[dgn]
        span = (e["t1"] or 0) - (e["t0"] or 0)
        return (e["n"] - 1) / span if span > 0.5 else 0.0


class Monitor:
    def __init__(self, args):
        self.args = args
        self.nodes = {}
        self.dm_baseline = defaultdict(set)
        self.dm_reported = set()
        self.started = time.time()
        self.baselining = args.watch_dm
        self.jitter_dgn = int(args.jitter, 16) if args.jitter else None
        self.sa_filter = None
        if args.sa:
            self.sa_filter = {int(x, 16) for x in args.sa.split(",")}

        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.sock.bind((args.iface,))
        self.sock.settimeout(1.0)

    def run(self):
        last = 0.0
        while True:
            try:
                self._handle(self.sock.recv(16))
            except socket.timeout:
                pass
            now = time.time()
            if self.baselining and (now - self.started) >= self.args.baseline:
                self.baselining = False
                print("\n=== baseline done (%d nodes) -- watching for changes ===\n"
                      % len(self.dm_baseline))
            if (not self.args.watch_dm and not self.args.stream
                    and not self.jitter_dgn and (now - last) >= self.args.refresh):
                self._table()
                last = now

    def _handle(self, frame):
        can_id, dlc = struct.unpack_from("<IB", frame, 0)
        if not (can_id & CAN_EFF_FLAG):
            return
        can_id &= CAN_EFF_MASK
        sa = can_id & 0xFF
        dgn = (can_id >> 8) & 0x1FFFF
        data = frame[8:8 + dlc]
        t = time.time()

        if self.sa_filter and sa not in self.sa_filter:
            return

        node = self.nodes.setdefault(sa, Node(sa))
        node.observe(dgn, data, t)

        if self.jitter_dgn is not None and dgn == self.jitter_dgn:
            self._jitter(sa, node, dgn, t)
        elif self.args.stream:
            label, dec, conf = decode(dgn, data)
            print("%s  %02X  %-22s %-16s %s" % (
                time.strftime("%H:%M:%S"), sa, label, data.hex(), dec or ""))
        elif self.args.watch_dm and dgn in (0x1FECA, 0x0FECA):
            self._dm(sa, data, t)

    def _jitter(self, sa, node, dgn, t):
        gaps = node.dgns[dgn]["gaps"]
        if not gaps:
            return
        label, dec, _ = decode(dgn, node.dgns[dgn]["last"])
        print("%s  %02X  %s  gap=%.3fs  (min %.3f / mean %.3f / max %.3f, n=%d)"
              % (time.strftime("%H:%M:%S"), sa, label, gaps[-1],
                 min(gaps), sum(gaps) / len(gaps), max(gaps), len(gaps)))
        if dec:
            print("        %s" % dec)

    def _dm(self, sa, data, t):
        payload = bytes(data)
        if self.baselining:
            self.dm_baseline[sa].add(payload)
            return
        if payload in self.dm_baseline[sa] or (sa, payload) in self.dm_reported:
            return
        self.dm_reported.add((sa, payload))
        _, dec, _ = decode(0x1FECA, data)
        print("[%s] DM_RV CHANGE  sa=%02X  %s" % (
            time.strftime("%H:%M:%S"), sa, payload.hex()))
        print("    %s" % dec)
        print("    baseline: %s\n" % ", ".join(
            sorted(x.hex() for x in self.dm_baseline[sa])))

    def _table(self):
        print("\033[2J\033[H", end="")
        print("RV-C inventory   %s   %d nodes   [SPEC]=documented  "
              "[DERIVED]=inferred  [GUESS]=partial\n"
              % (time.strftime("%H:%M:%S"), len(self.nodes)))
        for sa in sorted(self.nodes):
            node = self.nodes[sa]
            hdr = "SA 0x%02X" % sa
            if node.dsa is not None:
                hdr += "   DSA %d (%s)" % (
                    node.dsa, DSA_NAMES.get(node.dsa, "?"))
            if node.fault:
                hdr += "   *** DM_RV REPORTING A FAULT ***"
            print(hdr)
            for dgn in sorted(node.dgns):
                e = node.dgns[dgn]
                label, dec, conf = decode(dgn, e["last"])
                tag = ("[%s]" % conf) if conf else ""
                print("    %-22s %5.2f Hz  n=%-5d %-17s %s" % (
                    label, node.rate(dgn), e["n"], e["last"].hex(), tag))
                if dec:
                    print("        %s" % dec)
            print()
        sys.stdout.flush()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--iface", default="can0")
    p.add_argument("--sa", help="comma-separated hex source addresses to keep")
    p.add_argument("--stream", action="store_true", help="decode every frame live")
    p.add_argument("--watch-dm", action="store_true",
                   help="baseline DM_RV, then report deviations")
    p.add_argument("--jitter", help="hex DGN; report inter-arrival times")
    p.add_argument("--baseline", type=int, default=30)
    p.add_argument("--refresh", type=float, default=10.0)
    args = p.parse_args()

    m = Monitor(args)
    if args.watch_dm:
        print("Baselining DM_RV for %d s -- keep the system known-good.\n"
              % args.baseline)
    try:
        m.run()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
