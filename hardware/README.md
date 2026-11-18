# VIGIL hardware

Two devices: the **VIGIL box** (relay, one per camera) and the **bathroom sensor**
(a ceiling radar, no camera). They do not plug into each other. Each is on the home Wi-Fi
and reports to VIGIL on its own, so the sensor can sit in the bathroom while the box sits
by the router.

## VIGIL box (prototype 2)

`vigil_box.py` builds a printable case for a Raspberry Pi Zero 2 W:
88 x 58 x 24 mm closed, two parts, no supports, no screws.

```
pip install manifold3d trimesh
python3 hardware/vigil_box.py
```

It writes `vigil_box_base.stl` (print open side up), `vigil_box_lid.stl` (print top side
down) and `vigil_box_preview.stl` (assembled, lid lifted, board stand-in, for looking at).

Inside: the Pi drops onto four 2.4 mm pegs through its mounting holes (so it cannot slide),
and three tubes under the lid press it onto its posts with a 0.15 mm gap (so it cannot lift
or rattle). microSD reachable from the end, power and a second micro-USB (for an Ethernet
adapter) out the front, vents on the back, a 0.4 mm light window, keyholes for wall mounting.
Set `PEGS = False` for M2.5 screw posts instead.

Checked in the model: no part of the lid touches the base or the board, and the board
clears every wall. Board, hole and connector positions follow the Raspberry Pi Zero
mechanical drawing (65 x 30 mm, holes 3.5 mm in from each edge, 58 x 23 mm apart,
connectors 12.4 / 41.4 / 54.0 mm from the SD-card end). Not yet checked against a printed
part, and the LED position is the least certain number.

### Printing

- **Material:** PETG for anything that goes into a home (handles warm rooms and summer
  cars; PLA softens near 55 C). PLA is fine for the first fit test. Lid in white or light
  grey so the status light shows through.
- **Settings:** 0.4 mm nozzle, 0.2 mm layers, 3 walls, 15-20% infill, no supports, brim
  not needed. About 2 hours and under 55 g for both parts.
- **First print checklist:** pegs go through the board's holes without forcing; the lid
  snaps on and the board does not move when you shake it; both micro-USB plugs go in fully;
  the microSD card comes out; the green light shows through the lid.
- **If the fit is off:** change the number at the top of `vigil_box.py` (for example `FIT`
  for the lid, `PEG_D` for the pegs, `CLAMP_GAP` for the clamp) and print again. Printers
  differ by about 0.1-0.2 mm, so expect one or two rounds.

### Making more than a few

| How many | Best way | Rough cost per box |
| --- | --- | --- |
| 1-50 (pilots) | Your own printer or a school/library printer, PETG | under $1 of plastic |
| 50-500 | Print service (JLCPCB, Craftcloud, Xometry) in MJF nylon or SLA resin: smoother, consistent | about $3-8 |
| 1,000+ | Injection moulding; tooling is a one-time cost of a few thousand dollars | under $1 |

An off-the-shelf Pi Zero case (about $5-8) is a fine stopgap but has no VIGIL mark,
light window or wall keyholes.

## Bathroom sensor

See `bathroom-sensor/README.md`.
