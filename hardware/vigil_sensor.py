#!/usr/bin/env python3
"""VIGIL bathroom sensor: a ceiling puck for the Seeed MR60FDA2 60 GHz radar kit (XIAO ESP32-C6).
Writes vigil_sensor_base.stl, vigil_sensor_cover.stl and vigil_sensor_preview.stl next to this file.

    pip install manifold3d trimesh
    python3 hardware/vigil_sensor.py

Two parts. The BASE screws or sticks to the ceiling and holds the kit, radar facing down. The
COVER twists on (a bayonet, like a smoke alarm: push up, turn 20 degrees, it drops into a seat
and cannot turn back by itself) so it can come off without touching the screws.

Why it looks the way it does:
- Radome: the cover's face is 1.5 mm thick over the radar. That is half a wavelength of 60 GHz
  in PLA/PETG/nylon (5 mm / (2 x sqrt(2.7)) = 1.5 mm), the thickness that lets the most signal
  through. Do not paint it or use metallic or carbon-filled plastic.
- Splash: no openings face down or sideways. Water runs off the domed face; air breathes through
  the gap at the ceiling. The power cable leaves at the ceiling line.
- Light: the kit's status LED shines through a 0.5 mm thin spot, so the face stays sealed.

Kit size measured from Seeed's scaled photo (board 39 x 29 mm, radar module out to 47.3 mm, two
2 mm holes): PRINT ONE AND TEST-FIT with a real kit before making more.
"""
import math
import os

import numpy as np
import trimesh
from manifold3d import CrossSection, Manifold

HERE = os.path.dirname(os.path.abspath(__file__))
SEG = 160

# ---- kit (board frame: x from the USB-C end, y across, front = radar side) -----------------
BRD_L, BRD_W, BRD_T = 39.1, 29.0, 1.6            # black carrier board
MOD_X0, MOD_X1, MOD_Y0, MOD_Y1 = 15.9, 47.3, 2.1, 26.3   # radar module on the front
HOLES = [(9.6, 2.8), (9.6, 26.2)]                # 2 mm holes, M2 screws
LED = (3.6, 23.7)                                # RGB status LED, front
USB_Y, USB_OVERHANG = 14.4, 2.2                  # power USB-C, front side, at x = 0
BACK_STACK = 13.0                                # XIAO on its headers, behind the board
FRONT_STACK = 3.5                                # radar shield in front of the board

# ---- puck (ceiling frame: z = 0 at the ceiling, z grows downward) ---------------------------
R_OUT = 41.0                     # 82 mm across
COVER_WALL, RADOME = 2.8, 1.5
R_IN = R_OUT - COVER_WALL        # 38.2
FILLET = 7.0
RIM_Z = 0.6                      # breathing gap at the ceiling
PLATE = 2.4
SKIRT_T, SKIRT_FIT = 1.8, 0.25
R_SKIRT = R_IN - SKIRT_FIT       # 37.95 outside of the base skirt
SKIRT_Z1 = 10.0
BOARD_BACK_Z = PLATE + BACK_STACK                  # 15.4
BOARD_FRONT_Z = BOARD_BACK_Z + BRD_T               # 17.0
RADOME_IN_Z = BOARD_FRONT_Z + FRONT_STACK + 1.5    # 22.0
BOTTOM_Z = RADOME_IN_Z + RADOME                    # 23.5
PLUG_POCKET = 15.0                                 # room for a right-angle USB-C plug
XB = -(BRD_L + (MOD_X1 - BRD_L) + USB_OVERHANG + PLUG_POCKET) / 2 + USB_OVERHANG + PLUG_POCKET
YB = -BRD_W / 2

# bayonet
PINS = (90.0, 210.0, 330.0)
PIN_ARC, PIN_R, PIN_Z0, PIN_Z1 = 4.0, 1.2, 5.0, 7.0
TWIST = 20.0
SLOT_CLR = 0.4
SEAT = 0.6
CABLE_ANGLE = 180.0              # where the cable leaves, in the base's frame
CABLE_W = 5.0


def b2w(x, y):
    """Board coordinates to puck coordinates (front faces down)."""
    return XB + x, YB + y


def cyl(x, y, z, d, h, seg=48):
    return Manifold.cylinder(h, d / 2.0, circular_segments=seg).translate((x, y, z))


def box(x, y, z, sx, sy, sz):
    return Manifold.cube((sx, sy, sz)).translate((x, y, z))


def arc_pts(cx, cz, r, a0, a1, n=24):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cz + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def revolve(pts):
    return Manifold.revolve(CrossSection([pts]), circular_segments=SEG)


def sector(r0, r1, a0, a1, z0, z1):
    """An annular sector between angles a0..a1 (degrees), radii r0..r1, heights z0..z1."""
    n = max(4, int(abs(a1 - a0) / 2))
    pts = [(r1 * math.cos(math.radians(a0 + (a1 - a0) * i / n)), r1 * math.sin(math.radians(a0 + (a1 - a0) * i / n)))
           for i in range(n + 1)]
    pts += [(r0 * math.cos(math.radians(a1 - (a1 - a0) * i / n)), r0 * math.sin(math.radians(a1 - (a1 - a0) * i / n)))
            for i in range(n + 1)]
    return Manifold.extrude(CrossSection([pts]), z1 - z0).translate((0, 0, z0))


# ---------------------------------------------------------------------------------- base
def base():
    out = cyl(0, 0, 0, 2 * R_SKIRT, PLATE, SEG)
    out += cyl(0, 0, PLATE - 0.01, 2 * R_SKIRT, SKIRT_Z1 - PLATE, SEG) - cyl(0, 0, PLATE - 0.5, 2 * (R_SKIRT - SKIRT_T), SKIRT_Z1, SEG)
    for a in PINS:                                                    # bayonet pins
        out += sector(R_SKIRT - 0.5, R_SKIRT + PIN_R, a - PIN_ARC / 2 / R_SKIRT * 180 / math.pi,
                      a + PIN_ARC / 2 / R_SKIRT * 180 / math.pi, PIN_Z0, PIN_Z1)
    for hx, hy in HOLES:                                              # posts the kit screws onto
        x, y = b2w(hx, hy)
        out += cyl(x, y, PLATE - 0.01, 4.6, BOARD_BACK_Z - PLATE + 0.01)
        out -= cyl(x, y, PLATE + 1.0, 1.6, BOARD_BACK_Z)
    for ry in (MOD_Y0 + 1.6, MOD_Y1 - 1.6):                           # rests under the radar module's far end
        x, y = b2w(BRD_L + 3.2, ry)
        out += cyl(x, y, PLATE - 0.01, 3.0, BOARD_FRONT_Z - PLATE + 0.01)
    ca = math.radians(CABLE_ANGLE)                                    # cable exit at the ceiling line
    cx, cy = math.cos(ca), math.sin(ca)
    exit_ = box(0, -CABLE_W / 2, -0.01, R_SKIRT + 2, CABLE_W, 6.0)
    out -= exit_.rotate((0, 0, CABLE_ANGLE))
    for a in (0.0, 180.0):                                            # two countersunk ceiling screws (No. 6)
        x, y = 26 * math.cos(math.radians(a + 90)), 26 * math.sin(math.radians(a + 90))
        out -= cyl(x, y, -0.01, 3.6, PLATE + 0.02)
        out -= Manifold.cylinder(2.0, 3.6, 1.8, circular_segments=48).translate((x, y, PLATE - 2.0 + 0.01))
    return out


# ---------------------------------------------------------------------------------- cover
def cover_frame():
    outer = [(0, RIM_Z), (R_OUT - 0.8, RIM_Z), (R_OUT, RIM_Z + 0.8)]
    outer += arc_pts(R_OUT - FILLET, BOTTOM_Z - FILLET, FILLET, 0, 90)
    outer += [(0, BOTTOM_Z)]
    fi = FILLET - 2.6
    inner = [(0, RIM_Z - 1), (R_IN, RIM_Z - 1)]
    inner += arc_pts(R_IN - fi, RADOME_IN_Z - fi, fi, 0, 90)
    inner += [(0, RADOME_IN_Z)]
    out = revolve(outer) - revolve(inner)
    deg = lambda mm: mm / R_IN * 180 / math.pi
    half = deg(PIN_ARC / 2 + SLOT_CLR)
    for a in PINS:                                                    # L-shaped bayonet slots
        out -= sector(R_IN - 0.5, R_SKIRT + PIN_R + SLOT_CLR, a - half, a + half, RIM_Z - 1, PIN_Z1 + SLOT_CLR)
        out -= sector(R_IN - 0.5, R_SKIRT + PIN_R + SLOT_CLR, a - TWIST - half, a + half,
                      PIN_Z0 - SLOT_CLR - SEAT * 0, PIN_Z1 + SLOT_CLR)
        out -= sector(R_IN - 0.5, R_SKIRT + PIN_R + SLOT_CLR, a - TWIST - half, a - TWIST + half,
                      PIN_Z0 - SLOT_CLR, PIN_Z1 + SLOT_CLR + SEAT)    # the seat it drops into
    # cable notch at the rim, wide enough for the 20 degree turn
    out -= sector(R_IN - 1, R_OUT + 1, CABLE_ANGLE - TWIST - deg(CABLE_W / 2 + 1), CABLE_ANGLE + deg(CABLE_W / 2 + 1),
                  RIM_Z - 1, RIM_Z + 4.6)
    return out


def cover_locked():
    """The cover as it sits on the base once turned and locked (cover frame turned by +TWIST)."""
    c = cover_frame().rotate((0, 0, TWIST))
    # features fixed to the room: status-light thin spot over the LED, and the V mark
    lx, ly = b2w(*LED)
    c -= cyl(lx, ly, RADOME_IN_Z - 0.01, 4.0, RADOME - 0.5 + 0.01)
    d, arm = 0.4, 9.0
    for sx in (-1, 1):
        a = cyl(sx * arm * 0.62, -arm * 0.62, BOTTOM_Z - d, 3.2, d + 0.1)
        b = cyl(0, arm * 0.62, BOTTOM_Z - d, 3.2, d + 0.1)
        c -= Manifold.batch_hull([a, b])
    return c


def kit_standin():
    z = BOARD_BACK_Z
    k = box(*b2w(0, 0), z, BRD_L, BRD_W, BRD_T)
    k += box(*b2w(MOD_X0, MOD_Y0), BOARD_FRONT_Z, MOD_X1 - MOD_X0, MOD_Y1 - MOD_Y0, 1.0)
    k += box(*b2w(MOD_X0 + 0.5, MOD_Y0 + 0.5), BOARD_FRONT_Z + 1.0, 22.5, MOD_Y1 - MOD_Y0 - 1.0, 2.5)
    k += box(*b2w(6.0, 6.0), z - BACK_STACK + 0.6, 21.0, 17.5, BACK_STACK - 0.6)        # XIAO stack
    k += box(*b2w(-USB_OVERHANG, USB_Y - 4.5), BOARD_FRONT_Z, 7.5, 9.0, 3.2)             # USB-C
    for hx, hy in HOLES:
        k -= cyl(*b2w(hx, hy), z - 1, 2.0, 5)
    return k


def save(m, name):
    mesh = m.to_mesh()
    t = trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3], faces=np.asarray(mesh.tri_verts), process=True)
    t.export(os.path.join(HERE, name))
    return t


if __name__ == "__main__":
    b, c, k = base(), cover_locked(), kit_standin()
    clash = [(n, (x ^ y).volume()) for n, x, y in (("cover/base", c, b), ("cover/kit", c, k), ("base/kit", b, k))]
    for n, v in clash:
        print("overlap %-10s %.3f mm3" % (n, v))
    flip = lambda m: m.mirror((0, 0, 1)).translate((0, 0, BOTTOM_Z))
    tb = save(b, "vigil_sensor_base.stl")                                   # plate on the bed
    tc = save(flip(c), "vigil_sensor_cover.stl")                            # face on the bed
    save(b.mirror((0, 0, 1)) + c.mirror((0, 0, 1)).translate((0, 0, -18)) + k.mirror((0, 0, 1)),
         "vigil_sensor_preview.stl")                                        # hanging, cover lowered
    for n, t in (("base", tb), ("cover", tc)):
        print("%-5s %5.1f x %5.1f x %5.1f mm   watertight=%s   %.1f cm3" % (n, *t.extents, t.is_watertight, t.volume / 1000))
    print("closed puck: %.0f mm across, %.1f mm deep" % (2 * R_OUT, BOTTOM_Z))
