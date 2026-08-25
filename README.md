# dbus-rvc-renogy

Restores managed-battery and DVCC support in Victron Venus OS for Renogy REGO
batteries connected over RV-C.

## Scope

This bridge is specifically validated with the Renogy REGO
`RBT12400LFPL-SHBT`. It is not a universal RV-C battery driver.

The defaults match the tested three-pack, 1200 Ah installation:

- RV-C interface: `can0`
- Renogy bank aggregator source address: automatically discovered
- Venus device instance: `1`
- Maximum accepted charge voltage: `14.6 V`
- Maximum accepted charge current: `300 A`

Confirm these values from a passive bus capture before using the bridge with a
different REGO topology. Runtime overrides are documented in
[`config.example`](config.example).

## Why this exists

Venus OS 3.32, using `dbus-rv-c 1.06.00`, created a generic managed-battery
service for the REGO bank:

```text
com.victronenergy.battery.socketcan_can0_vi1_uc<unique-number>_di1
ProductId       0xB007
ProductName     CAN-bus BMS battery
DeviceInstance  1
```

Venus OS 3.75 uses `dbus-rv-c 1.11.00`. It identifies the manufacturer, model,
and serial numbers of the Renogy nodes but does not create a battery service.
The 1.11 binary contains named Lithionics and Battle Born battery profiles and
no Renogy profile. Supported RV-C devices on the same bus continue to work.

The regression is therefore the removal of the old generic-battery fallback,
not malformed Renogy status traffic.

## REGO bank interface

In the validated topology, one Renogy node acts as the bank aggregator and
publishes standard RV-C messages for DC source instance 1 and priority 120.
The aggregate role moved from source address `0x8D` to `0x8E` after a bank
restart, so the bridge discovers the publisher from its complete bank-level
frame set and Battery SOC priority `120` rather than pinning or restricting its
source address. The GX's priority-`119` RV-C rebroadcast is excluded by
identity instead of by its current address:

| DGN | Message | Observed value/rate |
| --- | --- | --- |
| `0x1FFFD` | `DC_SOURCE_STATUS_1` | Voltage/current, 2 Hz |
| `0x1FFFC` | `DC_SOURCE_STATUS_2` | Temperature/SOC, 2 Hz |
| `0x1FFFB` | `DC_SOURCE_STATUS_3` | SOH/remaining capacity, 2 Hz |
| `0x1FEC9` | `DC_SOURCE_STATUS_4` | Charge limits, 0.2 Hz |
| `0x1FEC7` | `DC_SOURCE_STATUS_6` | Status flags, 0.5 Hz |
| `0x1FEA5` | `DC_SOURCE_STATUS_11` | Full capacity/power, 1 Hz |

The bridge consumes only these aggregate messages. It does not combine the
per-pack `BATTERY_STATUS_*` family: only two of the three nodes in the tested
topology publish it, while the working Venus OS 3.32 driver demonstrably used
the aggregator's bank-level charge limit.

## Safety behavior

`dbus-rvc-renogy.py` is read-only on CAN. It:

- waits for live measurements and valid charge limits before registering its
  D-Bus BMS service, avoiding a disconnected service during GX startup;
- rebinds its read-only CAN socket if boot-time interface setup leaves it
  without fresh aggregate measurements;
- discovers the priority-120 Renogy aggregate publisher across the valid RV-C
  address space and follows an aggregate-role change after the previous source
  becomes silent;
- publishes zero charge current until measurement and charge-limit streams are
  both fresh and valid;
- publishes zero charge current if either stream becomes stale;
- clears voltage, current, power, temperature, SOC, SOH, and capacity when the
  measurement stream becomes stale, so the GUI and VRM do not show old data as
  live;
- rejects implausible charge-voltage limits and clamps configured maxima;
- converts REGO/RV-C current to Venus OS's charging-positive convention; and
- publishes full installed capacity from `DC_SOURCE_STATUS_11`, not remaining
  capacity from `DC_SOURCE_STATUS_3`.

Starting the service is a live DVCC action. With battery and BMS selection set
to `Automatic`, Venus normally makes it the active battery and active BMS
immediately.

After the service has registered, it deliberately retains the last validated
charge-voltage limit while publishing `/Connected = 0` and a zero charge-current
limit during a data outage. Venus uses the presence of the charge-voltage path
to classify a service as a BMS; clearing it can trigger Lost BMS handling and
charger error 67. Retaining only that previously validated voltage keeps the
BMS identity stable while the zero current limit fails charging closed.

## Offline tests

```sh
python3 -m unittest -v test_dbus_rvc_renogy.py test_rvc_inventory.py test_package_layout.py
python3 -m py_compile dbus-rvc-renogy.py rvc-inventory.py
```

The tests replay captured aggregate frames and cover polarity, capacity,
limits, stale-data fail-safe behavior, invalid limits, automatic aggregate
discovery and handover, rejection of the GX's own RV-C rebroadcast, the Venus
3.32 D-Bus contract, per-pack diagnostic decoding, and the persistent service
package.

## Passive per-battery diagnostics

`rvc-inventory.py` is a separate read-only diagnostic tool. It does not alter
the bridge or transmit CAN frames. To compare the three REGO battery nodes:

```sh
/data/dbus-rvc-renogy/rvc-inventory.py --sa 8C,8D,8E
```

The inventory decodes each reported battery's charge/discharge contactor
state, current, SOC, capacity, standard limit flags, and `DM_RV` lamp status.
Renogy diagnostics whose SPN is outside RV-C's standardized Battery service
point table remain identified numerically; their meaning must not be guessed
into the live Venus alarm paths.

To capture a warning transition, first start a 30-second baseline while the
system is stable, then reproduce the change:

```sh
/data/dbus-rvc-renogy/rvc-inventory.py --sa 8C,8D,8E --watch-dm
```

The REGO manual distinguishes solid, slow-flash, fast-flash, strobe, and
double-flash yellow patterns. Record the exact physical pattern along with the
matching source address and decoded DM_RV line.

Protocol and indicator references:

- [RVIA RV-C Layer specification, July 31, 2025](https://www.rvia.org/system/files/media/file/RV-C%20Specification%20Full%20Layer%206-31-25_Final_0.pdf)
- [Renogy REGO 12V 400Ah battery manual](https://ca.renogy.com/content/manual/RBT12400LFPL-SHBT-Manual.pdf)

## Optional configuration

The service loads `/data/dbus-rvc-renogy/config` when present. Start from
`config.example`; values must be exported so the Python process inherits them:

```sh
cp /data/dbus-rvc-renogy/config.example /data/dbus-rvc-renogy/config
```

Supported variables are:

```sh
export RVC_RENOGY_CAN_INTERFACE=can0
export RVC_RENOGY_SOURCE_ADDRESS=auto
export RVC_RENOGY_DEVICE_INSTANCE=1
export RVC_RENOGY_CVL_CEILING=14.6
export RVC_RENOGY_CCL_CEILING=300.0
```

Source-address discovery is automatic by default. The REGO bank can elect a
different physical battery as its aggregate publisher after a restart, so a
fixed address such as `0x8D` is suitable only as a temporary diagnostic
override. Fixed addresses may use `0x8D`, bare hexadecimal `8D`, or decimal
syntax. The selected source must still identify itself as priority 120, which
prevents accidentally feeding the bridge from the GX's priority-119 RV-C
rebroadcast.

## Controlled first run

Do not install the service persistently yet. From the cloned repository on a
workstation, copy only the bridge script to the GX device:

```sh
cd dbus-rvc-renogy
ssh root@venus.local 'mkdir -p /data/dbus-rvc-renogy && chmod 755 /data/dbus-rvc-renogy'
scp dbus-rvc-renogy.py root@venus.local:/data/dbus-rvc-renogy/
```

Log in, make the script executable, and run it in the foreground while the
native `dbus-rv-c` remains running:

```sh
ssh root@venus.local
chmod 755 /data/dbus-rvc-renogy/dbus-rvc-renogy.py
/data/dbus-rvc-renogy/dbus-rvc-renogy.py
```

Supervise the system and be ready to press `Ctrl-C`; doing so removes the
D-Bus service and returns control to the previous automatic selection.

From a second shell, inspect the bridge and system selection:

```sh
dbus -y | grep com.victronenergy.battery
dbus -y com.victronenergy.battery.rvc_renogy_can0 / GetValue
dbus -y com.victronenergy.system / GetValue |
    grep -E 'Active(Battery|Bms)|Available(Battery|Bms)'
```

Expected values for the validated three-pack installation include:

```text
ProductId                 45063 (0xB007)
ProductName               CAN-bus BMS battery
DeviceInstance            1
Connected                 1
Capacity                  1200
Info/MaxChargeVoltage     14.4
Info/MaxChargeCurrent     0..300
```

Current must be positive while the bank is charging and negative while it is
discharging. Stop immediately if polarity, capacity, limits, or temperature
are wrong.

## Verify DVCC consumption

```sh
for path in \
    /Control/Dvcc \
    /Control/BmsParameters \
    /Control/MaxChargeCurrent \
    /Control/EffectiveChargeVoltage \
    /Dc/Battery/ChargeVoltage \
    /Dvcc/Alarms/FirmwareInsufficient \
    /Dvcc/Alarms/MultipleBatteries
do
    echo "$path"
    dbus -y com.victronenergy.system "$path" GetValue
done
```

Expected results are `True` or `1` for DVCC and BMS parameter control,
approximately the BMS-requested voltage for the charge-voltage paths, and `0`
for both DVCC alarms. `/Control/MaxChargeCurrent` is a control-active flag, not
the ampere limit.

## Persistent installation on Venus OS

The persistent package lives under `/data`, which survives ordinary reboots
and Venus OS firmware updates. Venus rebuilds `/service` as a runtime overlay
on every boot, so a link created there manually does not persist. The installer
adds a marked command to the supported late-boot hook `/data/rc.local`; that
command recreates the runit link after `/service` is ready. Existing commands
in `rc.local` are preserved.

Ensure **Settings → General → Modification checks → All modifications enabled**
is enabled. Venus OS disables the local boot hooks when modifications are
disabled. See Victron's
[root-access documentation](https://www.victronenergy.com/live/ccgx%3Aroot_access)
for the boot-hook and `/service` overlay behavior.

Stop the foreground bridge with `Ctrl-C`. From a workstation clone, run the
tests, then deploy and verify the current checkout with one command:

```sh
cd dbus-rvc-renogy
git pull --ff-only
python3 -m unittest -v test_dbus_rvc_renogy.py test_rvc_inventory.py test_package_layout.py
./deploy.sh root@venus.local
```

The deployment uses `rsync` over SSH, preserves the remote `config`, runs the
installer to restart the supervised service, and waits until D-Bus reports the
version from the local `version` file. It does not copy Git history or delete
operator-created files on the GX device.

If a previous diagnostic session pinned `RVC_RENOGY_SOURCE_ADDRESS`, return it
to automatic discovery during deployment:

```sh
./deploy.sh --auto-source root@venus.local
```

The target defaults to `root@venus.local` and can instead be supplied through
`RVC_RENOGY_TARGET`. Both the workstation and GX device must provide `rsync`
and `ssh`.

The resulting section in `/data/rc.local` is:

```sh
# BEGIN dbus-rvc-renogy
/data/dbus-rvc-renogy/install-service.sh --boot
# END dbus-rvc-renogy
```

Other commands already present in `rc.local` remain unchanged. If the file has
an `exit 0` line, the installer places its block before it.

Confirm that the installed process reports the expected version:

```sh
dbus -y com.victronenergy.battery.rvc_renogy_can0 \
    /Mgmt/ProcessVersion GetValue
```

To uninstall, remove the supervised-service link and this package's marked
boot block while leaving other `rc.local` commands and the package files under
`/data` intact:

```sh
/data/dbus-rvc-renogy/uninstall-service.sh
```

After installation, reboot once and verify that the stock Venus boot hook
restored the service without manual intervention:

```sh
reboot
# After the GX device is reachable again:
svstat /service/dbus-rvc-renogy
dbus -y com.victronenergy.battery.rvc_renogy_can0 \
    /Mgmt/ProcessVersion GetValue
```

The same hook recreates the service following a Venus OS firmware update.

## License

This project is released under the [MIT License](LICENSE).
