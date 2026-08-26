# REGO-to-VE.Can adapter cable

This guide documents the adapter validated with the Renogy REGO G1
`RBT12400LFPL-SHBT` battery and a Victron GX VE.Can RJ45 port. **Other Renogy
models and revisions may use different LP16 pin assignments (see below Renogy link for details).** Verify every
conductor with a continuity meter before connecting the cable to powered
equipment.

## Wiring

| Signal | Victron VE.Can RJ45 | Standard T568B wire | Renogy cable wire color | Renogy REGO LP16 |
| --- | ---: | --- | --- | ---: |
| CAN-H | Pin 7 | White/brown | White | Pin 5 |
| CAN-L | Pin 8 | Brown | Blue | Pin 7 |
| CAN ground | Pin 3 | White/green | Black | Pin 2 |
| Others | Leave open | Leave open | Leave open | Leave open |

The Renogy colors above are those found in the validated cable. Renogy notes
that cable colors can vary, so the LP16 pin numbers—not wire colors—are the
authority. Pins 7 and 8 on a T568B RJ45 plug are one twisted pair and must stay
paired through the adapter.

## Connector orientation

### Renogy LP16: rear/solder-side view

> **Critical:** The diagram below looks at the pins from the **back of the
> connector**, as if you were soldering wires to them. Looking into the front,
> mating face of the connector produces the mirror image. Confusing these two
> views reverses the pin positions.

```text
REAR / SOLDER SIDE — latch and key at top

                 [ latch ]

                    7   6

                 5    4    3

                    2   1
```

For comparison, the front or mating face is mirrored:

```text
FRONT / MATING FACE — latch and key at top

                 [ latch ]

                    6   7

                 3    4    5

                    1   2
```

Use molded pin numbers when present and confirm the finished cable
electrically. Do not infer numbering solely from the apparent left-to-right
position of the contacts.

### RJ45: contact-side view

Hold the male RJ45 plug with its gold contacts facing you, its locking tab
away from you, and the cable exiting downward. Pins are numbered left to
right:

```text
CONTACT SIDE — locking tab on the far side

        1       2       3       4       5       6       7       8
      W/Orange Orange  W/Green  Blue   W/Blue   Green  W/Brown  Brown
                                                        CAN-H   CAN-L
                        CAN ground

                              cable
                                |
                                v
```

Patch-cable colors are not a substitute for testing: confirm the actual RJ45
pin at the splice with a continuity meter.

## Construction

1. Work with the GX device and the entire CAN network powered off.
2. Use a stranded, all-copper Cat5e-or-better pigtail. Avoid copper-clad
   aluminum cable.
3. Identify RJ45 pins 7, 8, and 3 by continuity. Do not rely only on jacket
   colors or connector orientation.
4. Identify REGO LP16 pins 5, 7, and 2 from the rear/solder side. Confirm the
   corresponding Renogy conductors by continuity.
5. Join only the three conductors in the wiring table. Cut back and individually
   insulate every unused conductor.
6. Keep CAN-H and CAN-L twisted together as close to the splice as practical.
   Stagger the three joints so they do not form one large rigid lump.
7. Use correctly sized sealed crimps, or make a mechanically secure soldered
   splice before applying solder. Cover each joint separately with adhesive-
   lined heat shrink, then add an overall sleeve that grips both cable jackets
   for strain relief.
8. Support the finished cable so its weight and vibration are not carried by
   the LP16 or RJ45 contacts.

## Tests before use

With the cable disconnected from all equipment:

- verify continuity from RJ45 pin 7 to LP16 pin 5;
- verify continuity from RJ45 pin 8 to LP16 pin 7;
- verify continuity from RJ45 pin 3 to LP16 pin 2;
- verify that none of those three circuits is shorted to another; and
- verify that every unused contact is open.

After connecting the complete but still unpowered CAN network, measure between
RJ45 pins 7 and 8. A correctly terminated network with one 120-ohm terminator
at each physical end should measure approximately 60 ohms. Never use an
ohmmeter on a powered network.

After power-up, confirm that the REGO nodes appear in `rvc-inventory.py`, the
bridge selects the expected aggregate publisher, and `/Connected` becomes `1`
before relying on the BMS service.

## References

- [Victron CAN-bus BMS cable pinout](https://www.victronenergy.com/live/battery_compatibility%3Acan-bus_bms-cable)
- [Renogy RV-C communication connections](https://www.renogy.com/blogs/learn-center/rv-c-communication-connections)

