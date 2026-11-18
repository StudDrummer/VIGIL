#!/usr/bin/env python3
"""VIGIL box: a printable case for the relay (Raspberry Pi Zero 2 W), with a spare bay for a
future sensor board. Writes vigil_box_base.stl, vigil_box_lid.stl and vigil_box_preview.stl
(base, lid lifted, and a stand-in for the board) next to this file.

    pip install manifold3d trimesh
    python3 hardware/vigil_box.py

Print: PETG or PLA, 0.2 mm layers, no supports. Base open side up, lid top side down.
Assembly: drop the Pi onto the four pegs, put the lid on; three tubes under the lid clamp the
board, so it cannot slide or rattle. No screws. (Set PEGS = False for M2.5 screw posts.)
Board positions follow the Raspberry Pi Zero mechanical drawing (65 x 30 mm board, holes
58 x 23 mm apart, connectors 12.4 / 41.4 / 54.0 mm from the SD-card end). PRINT ONE AND
TEST-FIT before making more; adjust the numbers at the top if a port is off.
"""
import os

import numpy as np
import trimesh
from manifold3d import Manifold

HERE = os.path.dirname(os.path.abspath(__file__))
SEG = 64

# ---- sizes (mm) -------------------------------------------------------------
L, W = 88.0, 58.0            # outside length and width
R = 11.0                     # corner radius
WALL, FLOOR = 2.0, 2.0
BASE_H = 19.0                # base height (outside)
LID_H = 5.0                  # lid thickness above the base
LIP_H, LIP_T, FIT = 3.0, 1.6, 0.2
CHAMFER = 1.8

PCB_L, PCB_W, PCB_T = 65.0, 30.0, 1.6
HOLES = [(3.5, 3.5), (61.5, 3.5), (3.5, 26.5), (61.5, 26.5)]
POST_H, POST_D, PILOT_D = 4.0, 5.6, 2.1          # M2.5 thread-forming screw (only when PEGS is False)
PEGS = True                                      # pegs through the board's holes; the lid clamps it. No screws needed
PEG_D, PEG_H = 2.4, 1.4                          # the holes are 2.75 mm; the board is 1.6 mm thick
CLAMP_OD, CLAMP_ID, CLAMP_GAP = 5.0, 2.9, 0.15   # tubes under the lid that press the board onto its posts
CLAMP_HOLES = [0, 2, 3]                          # three corners; the fourth sits beside the status light
USB_X, PWR_X = 41.4, 54.0                        # connector centres from the SD-card end
PLUG_W, PLUG_H = 12.5, 7.6                       # room for a micro-USB plug's moulding
SD_Y, SD_W, SD_H = 16.9, 15.0, 4.2               # microSD slot on the short edge
PCB_X0 = WALL + 9.0                              # board's SD-card end, from the outside left face
PCB_Y0 = WALL + 1.0                              # board's connector edge, from the outside front face


def rrect(length, width, radius, height, z=0.0, top_inset=0.0):
    """Rounded rectangle prism from (0,0). top_inset > 0 chamfers the top edge."""
    def layer(inset, h, zz):
        r = max(radius - inset, 0.5)
        cs = [Manifold.cylinder(h, r, circular_segments=SEG).translate((x, y, zz))
              for x in (inset + r, length - inset - r) for y in (inset + r, width - inset - r)]
        return Manifold.batch_hull(cs)
    if top_inset <= 0:
        return layer(0.0, height, z)
    lower = layer(0.0, height - top_inset, z)
    upper = layer(top_inset, 0.01, z + height - 0.01)
    return Manifold.batch_hull([lower, upper])


def box(x, y, z, sx, sy, sz):
    return Manifold.cube((sx, sy, sz)).translate((x, y, z))


def cyl(x, y, z, d, h):
    return Manifold.cylinder(h, d / 2.0, circular_segments=48).translate((x, y, z))


def slot(x, y, z, length, width, depth):
    """Rounded slot along x."""
    r = width / 2.0
    return Manifold.batch_hull([cyl(x + r, y + r, z, width, depth), cyl(x + length - r, y + r, z, width, depth)])


def base():
    body = rrect(L, W, R, BASE_H, top_inset=0.0)
    # bottom edge eased so it sits and prints cleanly
    foot = rrect(L, W, R, 1.2).translate((0, 0, 0))
    eased = Manifold.batch_hull([rrect(L - 2.4, W - 2.4, R - 1.2, 0.01).translate((1.2, 1.2, 0)), foot.translate((0, 0, 1.2))])
    body = eased + rrect(L, W, R, BASE_H - 1.2).translate((0, 0, 1.2))
    cavity = rrect(L - 2 * WALL, W - 2 * WALL, R - WALL, BASE_H).translate((WALL, WALL, FLOOR))
    out = body - cavity
    board_z = FLOOR + POST_H
    for hx, hy in HOLES:                                           # posts the board screws onto
        out += cyl(PCB_X0 + hx, PCB_Y0 + hy, FLOOR - 0.01, POST_D, POST_H + 0.01)
        if PEGS:
            out += cyl(PCB_X0 + hx, PCB_Y0 + hy, FLOOR + POST_H - 0.01, PEG_D, PEG_H + 0.01)
        else:
            out -= cyl(PCB_X0 + hx, PCB_Y0 + hy, FLOOR + 0.6, PILOT_D, POST_H)
    port_z = board_z + PCB_T + 1.5                                  # centre of the micro-USB sockets
    for cx in (USB_X, PWR_X):                                       # power in, and USB for a network adapter
        out -= box(PCB_X0 + cx - PLUG_W / 2, -1, port_z - PLUG_H / 2, PLUG_W, WALL + 2, PLUG_H)
    out -= box(-1, PCB_Y0 + SD_Y - SD_W / 2, board_z + PCB_T - 0.6, WALL + 2, SD_W, SD_H)      # microSD card
    for i in range(7):                                              # vents on the back wall
        out -= box(16.0 + i * 8.6, W - WALL - 1, 6.0, 3.0, WALL + 2, 9.0)
    for fx, fy in ((14, 12), (L - 14, 12), (14, W - 12), (L - 14, W - 12)):                     # rubber feet
        out -= cyl(fx, fy, -0.01, 9.0, 0.8)
    for kx in (L / 2 - 20, L / 2 + 20):                             # keyholes for two wall screws
        out -= cyl(kx, W / 2 + 4, -0.01, 8.0, FLOOR + 0.02)
        out -= slot(kx - 2.0, W / 2 + 4 - 9.0, -0.01, 4.0, 4.0, FLOOR + 0.02) + box(kx - 2.0, W / 2 - 5.0, -0.01, 4.0, 9.0, FLOOR + 0.02)
    return out


def lid():
    cap = rrect(L, W, R, LID_H, top_inset=CHAMFER)
    lip_o = rrect(L - 2 * (WALL + FIT), W - 2 * (WALL + FIT), R - WALL - FIT, LIP_H).translate((WALL + FIT, WALL + FIT, -LIP_H))
    lip_i = rrect(L - 2 * (WALL + FIT + LIP_T), W - 2 * (WALL + FIT + LIP_T), R - WALL - FIT - LIP_T, LIP_H + 1).translate(
        (WALL + FIT + LIP_T, WALL + FIT + LIP_T, -LIP_H - 0.5))
    out = cap + (lip_o - lip_i)
    # the lip must not block the plugs or the card
    for cx in (USB_X, PWR_X):
        out -= box(PCB_X0 + cx - PLUG_W / 2 - 0.5, 0, -LIP_H - 1, PLUG_W + 1, WALL + FIT + LIP_T + 1, LIP_H + 1)
    # clamps: with the lid on, these hold the board down on its posts so it cannot rattle, lift or slide
    board_top = FLOOR + POST_H + PCB_T - BASE_H                     # in lid coordinates (lid underside is z=0)
    if not PEGS:
        board_top += 1.8                                            # land on the screw heads instead
    for i in CLAMP_HOLES:
        hx, hy = HOLES[i]
        x, y = PCB_X0 + hx, PCB_Y0 + hy
        z0 = board_top + CLAMP_GAP
        out += cyl(x, y, z0, CLAMP_OD, -z0 + 0.01)
        out -= cyl(x, y, z0 - 0.01, CLAMP_ID, PEG_H + 0.6)
    # the mark: a V, sunk 0.6 mm into the top
    cx, cy, d = L / 2, W / 2, 0.6
    arm = 13.0
    for sx in (-1, 1):
        a = cyl(cx + sx * arm * 0.62, cy + arm * 0.62, LID_H - d, 4.4, d + 0.1)
        b = cyl(cx, cy - arm * 0.62, LID_H - d, 4.4, d + 0.1)
        out -= Manifold.batch_hull([a, b])
    # status light: a thin window over the board's LED, left 0.4 mm thick so light shows through a light filament
    out -= cyl(PCB_X0 + 60.5, PCB_Y0 + 6.5, -0.01, 3.2, LID_H - 0.4)
    return out


def board_standin():
    z = FLOOR + POST_H
    pcb = rrect(PCB_L, PCB_W, 3.0, PCB_T).translate((PCB_X0, PCB_Y0, z))
    for cx, w in ((12.4, 11.2), (USB_X, 7.6), (PWR_X, 7.6)):
        pcb += box(PCB_X0 + cx - w / 2, PCB_Y0 - 0.6, z + PCB_T, w, 6.0, 3.0)
    pcb += box(PCB_X0 + 1.0, PCB_Y0 + SD_Y - 6.0, z + PCB_T, 12.0, 12.0, 1.4)
    pcb += box(PCB_X0 + 22.0, PCB_Y0 + 9.0, z + PCB_T, 15.0, 15.0, 1.4)
    return pcb


def save(m, name):
    mesh = m.to_mesh()
    tm = trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3], faces=np.asarray(mesh.tri_verts), process=True)
    tm.export(os.path.join(HERE, name))
    return tm


if __name__ == "__main__":
    b, l = base(), lid()
    tb = save(b, "vigil_box_base.stl")
    tl = save(l.rotate((180, 0, 0)).translate((0, W, LID_H)), "vigil_box_lid.stl")          # top side down, ready to print
    save(b + l.translate((0, 0, BASE_H + 22)) + board_standin(), "vigil_box_preview.stl")
    for n, t in (("base", tb), ("lid", tl)):
        print("%-5s %5.1f x %5.1f x %5.1f mm   watertight=%s   %.1f cm3" % (n, *t.extents, t.is_watertight, t.volume / 1000))
    print("closed box: %.0f x %.0f x %.0f mm" % (L, W, BASE_H + LID_H))
