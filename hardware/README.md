# VIGIL hardware

Two devices: the **VIGIL box** (relay, one per camera) and the **bathroom sensor**
(a ceiling radar, no camera). They do not plug into each other. Each is on the home Wi-Fi
and reports to VIGIL on its own, so the sensor can sit in the bathroom while the box sits
by the router.

## VIGIL box (prototype 3)

`vigil_box.py` builds the case in two versions, two parts each, no supports, no screws:

| Version | Board | Closed size | Files |
| --- | --- | --- | --- |
| `pi3` | Raspberry Pi 3 Model B or B+ (what VIGIL runs on today) | 94.5 x 65.5 x 29.6 mm | `vigil_box_pi3_base.stl`, `vigil_box_pi3_lid.stl` |
| `zero2w` | Raspberry Pi Zero 2 W (when it is in stock) | 88 x 58 x 24 mm | `vigil_box_zero2w_base.stl`, `vigil_box_zero2w_lid.stl` |

```
pip install manifold3d trimesh
python3 hardware/vigil_box.py          # both; or: python3 hardware/vigil_box.py pi3
```

Print the base open side up and the lid top side down. The script also writes a `_preview.stl`
and checks that nothing in the lid touches the base or the board.

Inside: the board drops onto four 2.4 mm pegs through its mounting holes (so it cannot slide),
and tubes under the lid press it onto its posts with a 0.15 mm gap (so it cannot lift or
rattle). The Pi 3 version has openings for power, Ethernet and the microSD only (the USB,
HDMI and audio sockets stay inside); the Zero version has power, a second micro-USB for an
Ethernet adapter, and the microSD. Set `PEGS = False` for M2.5 screw posts instead.

What makes it more than a Pi case:
- **A status light that means something.** A light pipe carries the board's LED to one 1.2 mm
  dot on the lid. The relay software already drives it: steady = working, double blink = open
  the app to set up, slow blink = cannot find the camera, fast blink = no internet.
- **The setup code lives on the box.** A 28 mm pocket underneath takes the QR sticker.
- **Only the openings VIGIL uses** (power, a second USB for Ethernet, the microSD), no screws,
  keyholes for a wall, recessed feet, vents on the back.
- **Hollow lid** with a 1.8 mm skin: about 31 cm3 (Zero) or 41 cm3 (Pi 3) of plastic for both parts, which matters
  when a print service charges by weight.

Checked in the model: no part of the lid touches the base or the board, and the board
clears every wall. Board, hole and connector positions follow Raspberry Pi's mechanical
drawings for the Pi 3 B/B+ (85 x 56 mm, holes 58 x 49 mm apart) and the Zero 2 W (65 x 30 mm,
holes 58 x 23 mm apart). Not yet checked against a printed part. On the Pi 3 the green and red
lights sit side by side under the light pipe; the relay software turns the red one off.

### Printing

- **Material:** PETG for anything that goes into a home (handles warm rooms and summer
  cars; PLA softens near 55 C). PLA is fine for the first fit test. Any colour works:
  the status light comes through a pinhole (set `LIGHT = "skin"` for a sealed thin spot,
  which needs a light colour).
- **Settings:** 0.4 mm nozzle, 0.2 mm layers, 3 walls, 15-20% infill, no supports, brim
  not needed. About 2 hours and under 40 g for both parts.
- **First print checklist:** pegs go through the board's holes without forcing; the lid
  snaps on and the board does not move when you shake it; both micro-USB plugs go in fully;
  the microSD card comes out; the green light shows through the dot in the lid. If it does
  not, the LED is somewhere else on your board: measure it and change `leds` for that board in `BOARDS`.
- **If the fit is off:** change the number at the top of `vigil_box.py` (for example `FIT`
  for the lid, `PEG_D` for the pegs, `CLAMP_GAP` for the clamp) and print again. Printers
  differ by about 0.1-0.2 mm, so expect one or two rounds.

## Bathroom sensor puck (prototype 1)

`vigil_sensor.py` builds an 82 mm x 24 mm ceiling puck for the Seeed MR60FDA2 kit:
`vigil_sensor_base.stl` (plate on the bed), `vigil_sensor_cover.stl` (face on the bed),
`vigil_sensor_preview.stl`. The base screws to the ceiling (two No. 6 countersunk screws) and
holds the kit radar-down on two M2 x 6 mm thread-forming screws; the cover twists on with a
20 degree bayonet that drops into a seat. The face is 1.5 mm thick over the radar (half a
wavelength of 60 GHz in PLA/PETG/nylon); never paint it or print it in metallic or carbon-filled
plastic. The kit's size comes from Seeed's scaled photo, so fit a real kit before ordering more.
Wiring and firmware: `bathroom-sensor/README.md`.

## Getting them made

| Stage | Where | Notes |
| --- | --- | --- |
| First fit test | Bellevue Library Makerspace (KCLS), 1111 110th Ave NE | Free; Prusa printers; check their hours and intro class. Bring the STL files on a USB stick |
| 10-50 units | JLC3DP (jlc3dp.com) MJF PA12 nylon, dyed black, or Craftcloud to compare services | MJF was $0.275/g at JLC3DP: about $9 (Zero) to $12 (Pi 3) a box and $10 a puck before dye and shipping. Even, matte, strong, no layer lines |
| 1,000+ | Injection moulding | One-time mould cost of a few thousand dollars; well under $1 a part after |

Upload the STL files, pick the material, and the site quotes instantly. For the puck, MJF
nylon or SLA resin both pass 60 GHz; avoid "metal-look" or carbon materials.
