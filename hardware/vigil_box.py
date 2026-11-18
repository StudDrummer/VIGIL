#!/usr/bin/env python3
"""VIGIL box: a printable case for the relay computer. Two versions, one per board:

    pi3     Raspberry Pi 3 Model B or B+ (what VIGIL's relay boxes run on today)
    zero2w  Raspberry Pi Zero 2 W (smaller and cheaper, when it is in stock)

    pip install manifold3d trimesh
    python3 hardware/vigil_box.py            # both
    python3 hardware/vigil_box.py pi3        # one

Writes vigil_box_<board>_base.stl, _lid.stl and _preview.stl next to this file, and checks
that nothing in the lid touches the base or the board.

Print: PETG or PLA, 0.2 mm layers, no supports. Base open side up, lid top side down.
Purpose built in:
- A light pipe turns the board's green ACT light into one dot on the lid. The relay software
  drives it: steady = working, double blink = open the app to set it up, slow blink = cannot
  find the camera, fast blink = no internet. (On a Pi 3 it also turns the red power light off,
  so only the green one shows.)
- A pocket underneath for the setup-code sticker, so the code is never lost.
- No screws: the board drops onto pegs through its mounting holes and tubes under the lid clamp
  it when the lid closes, so it cannot slide, lift or rattle. (PEGS = False for screw posts.)
- Only the openings VIGIL uses (power, Ethernet or a USB adapter, the microSD), keyholes for a
  wall, recessed feet, vents.
Board, hole and connector positions follow Raspberry Pi's mechanical drawings. PRINT ONE AND
TEST-FIT before making more; adjust the numbers in BOARDS if something is off.
"""
import os
import sys

import numpy as np
import trimesh
from manifold3d import Manifold

HERE = os.path.dirname(os.path.abspath(__file__))
SEG = 64

# ---- shared sizes (mm) --------------------------------------------------------
R = 11.0                     # corner radius (set per board)
WALL, FLOOR = 2.0, 2.0
LID_H = 5.0                  # lid thickness above the base
LIP_H, LIP_T, FIT = 3.0, 1.6, 0.2
CHAMFER = 1.8
PCB_T = 1.6
POST_H, POST_D, PILOT_D = 4.0, 5.6, 2.1          # M2.5 thread-forming screw (only when PEGS is False)
PEGS = True
PEG_D, PEG_H = 2.4, 1.4                          # the holes are 2.75 mm
CLAMP_OD, CLAMP_ID, CLAMP_GAP = 5.0, 2.9, 0.15
LIGHT = "pinhole"                                # "pinhole": 1.2 mm dot (any colour); "skin": 0.4 mm thin spot (light colours)
QR_POCKET = 28.0
PLUG_W, PLUG_H = 12.5, 7.6                       # room for a micro-USB plug's moulding

# ---- boards -----------------------------------------------------------------------
# x along the board from its left short edge, y from its front long edge, z above its top face.
# ports: (edge, position along that edge, opening width, opening height, opening centre z)
# parts: stand-in components (x, y, size x, size y, height) for collision checks and the preview
BOARDS = {
    "pi3": dict(
        name="Raspberry Pi 3 Model B / B+", radius=6.0,
        size=(85.0, 56.0), holes=[(3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)],
        gaps=dict(left=3.0, right=2.5, front=3.0, back=2.5),
        tallest=16.0,                                        # USB sockets
        ports=[("front", 10.6, PLUG_W, PLUG_H, 1.5),        # micro-USB power
               ("right", 10.25, 17.0, 14.5, 6.75)],         # Ethernet, for buildings with bad Wi-Fi
        lip_clear=[("right", 19.0, 56.0)],                  # USB sockets reach the wall here
        sd=dict(edge="left", at=28.0, w=14.0, z0=-PCB_T - 3.0, h=4.6),   # card is under the board
        leds=[(1.1, 7.9), (1.1, 11.5)], pipe_z0=6.0,        # ACT and PWR side by side; pipe clears the display socket
        clamps=[0, 1, 2, 3],
        parts=[(66.0, 2.5, 21.1, 15.5, 13.5), (70.0, 21.5, 17.1, 15.0, 16.0), (70.0, 39.5, 17.1, 15.0, 16.0),
               (7.0, 50.0, 51.0, 5.0, 8.5), (6.6, -0.5, 8.0, 5.5, 3.0), (24.5, -0.5, 15.0, 11.5, 6.5),
               (50.0, -2.5, 7.0, 14.5, 6.0), (1.0, 13.2, 3.0, 17.5, 5.5), (44.0, 1.0, 3.0, 17.5, 5.5)]),
    "zero2w": dict(
        name="Raspberry Pi Zero 2 W", radius=11.0,
        size=(65.0, 30.0), holes=[(3.5, 3.5), (61.5, 3.5), (3.5, 26.5), (61.5, 26.5)],
        gaps=dict(left=9.0, right=10.0, front=1.0, back=23.0),
        tallest=3.0,
        ports=[("front", 41.4, PLUG_W, PLUG_H, 1.5), ("front", 54.0, PLUG_W, PLUG_H, 1.5)],
        lip_clear=[],
        sd=dict(edge="left", at=16.9, w=15.0, z0=-0.6, h=4.2),
        leds=[(60.5, 6.5)], pipe_z0=1.2,
        clamps=[0, 2, 3],
        parts=[(6.8, -0.6, 11.2, 6.0, 3.0), (37.6, -0.6, 7.6, 6.0, 3.0), (50.2, -0.6, 7.6, 6.0, 3.0),
               (1.0, 10.9, 12.0, 12.0, 1.4), (22.0, 9.0, 15.0, 15.0, 1.4)]),
}

B = None                      # the board being built
L = W = BASE_H = PCB_X0 = PCB_Y0 = 0.0


def use(key):
    global B, L, W, BASE_H, PCB_X0, PCB_Y0, R
    B = BOARDS[key]
    R = B["radius"]
    g = B["gaps"]
    bl, bw = B["size"]
    L = round(2 * WALL + g["left"] + bl + g["right"], 1)
    W = round(2 * WALL + g["front"] + bw + g["back"], 1)
    BASE_H = round(FLOOR + POST_H + PCB_T + B["tallest"] + 1.0, 1)
    BASE_H = max(BASE_H, 19.0)
    PCB_X0, PCB_Y0 = WALL + g["left"], WALL + g["front"]


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


def wall_cut(edge, at, w, z0, h, depth=WALL + 2):
    """A rectangular opening through one wall, `at` measured along the board from its own origin."""
    if edge == "front":
        return box(PCB_X0 + at - w / 2, -1, z0, w, depth, h)
    if edge == "back":
        return box(PCB_X0 + at - w / 2, W - depth + 1, z0, w, depth, h)
    if edge == "left":
        return box(-1, PCB_Y0 + at - w / 2, z0, depth, w, h)
    return box(L - depth + 1, PCB_Y0 + at - w / 2, z0, depth, w, h)


def base():
    body = rrect(L, W, R, BASE_H, top_inset=0.0)
    foot = rrect(L, W, R, 1.2)
    eased = Manifold.batch_hull([rrect(L - 2.4, W - 2.4, R - 1.2, 0.01).translate((1.2, 1.2, 0)), foot.translate((0, 0, 1.2))])
    body = eased + rrect(L, W, R, BASE_H - 1.2).translate((0, 0, 1.2))
    cavity = rrect(L - 2 * WALL, W - 2 * WALL, R - WALL, BASE_H).translate((WALL, WALL, FLOOR))
    out = body - cavity
    top = FLOOR + POST_H + PCB_T                                    # board's top face
    for hx, hy in B["holes"]:
        out += cyl(PCB_X0 + hx, PCB_Y0 + hy, FLOOR - 0.01, POST_D, POST_H + 0.01)
        if PEGS:
            out += cyl(PCB_X0 + hx, PCB_Y0 + hy, FLOOR + POST_H - 0.01, PEG_D, PEG_H + 0.01)
        else:
            out -= cyl(PCB_X0 + hx, PCB_Y0 + hy, FLOOR + 0.6, PILOT_D, POST_H)
    for edge, at, w, h, zc in B["ports"]:
        out -= wall_cut(edge, at, w, top + zc - h / 2, h)
    sd = B["sd"]
    out -= wall_cut(sd["edge"], sd["at"], sd["w"], top + sd["z0"], sd["h"])
    n = max(5, int((L - 32) / 8.6))                                 # vents on the back wall
    x0 = (L - (n - 1) * 8.6 - 3.0) / 2
    for i in range(n):
        out -= box(x0 + i * 8.6, W - WALL - 1, 6.0, 3.0, WALL + 2, min(9.0, BASE_H - 10.0))
    for fx, fy in ((14, 12), (L - 14, 12), (14, W - 12), (L - 14, W - 12)):
        out -= cyl(fx, fy, -0.01, 9.0, 0.8)
    q = QR_POCKET
    out -= box(L / 2 - q / 2, W / 2 - q / 2 - 2.0, -0.01, q, q, 0.5)
    for kx in (L / 2 - 20, L / 2 + 20):                             # keyholes for two wall screws
        out -= cyl(kx, W / 2 + 4, -0.01, 8.0, FLOOR + 0.02)
        out -= slot(kx - 2.0, W / 2 + 4 - 9.0, -0.01, 4.0, 4.0, FLOOR + 0.02) + box(kx - 2.0, W / 2 - 5.0, -0.01, 4.0, 9.0, FLOOR + 0.02)
    return out


def lid():
    cap = rrect(L, W, R, LID_H, top_inset=CHAMFER)
    inset = WALL + FIT + LIP_T
    lip_o = rrect(L - 2 * (WALL + FIT), W - 2 * (WALL + FIT), R - WALL - FIT, LIP_H).translate((WALL + FIT, WALL + FIT, -LIP_H))
    lip_i = rrect(L - 2 * inset, W - 2 * inset, R - inset, LIP_H + 1).translate((inset, inset, -LIP_H - 0.5))
    out = cap + (lip_o - lip_i)
    TOP = LID_H - 1.8                                               # hollow the lid, 1.8 mm top skin
    out -= rrect(L - 2 * inset, W - 2 * inset, R - inset, TOP + 0.01).translate((inset, inset, -0.01))
    for edge, at, w, h, zc in B["ports"]:                           # the lip must not block a plug
        if edge in ("front", "back"):
            out -= wall_cut(edge, at, w + 1, -LIP_H - 1, LIP_H + 1, depth=inset + 1.5)
        else:
            out -= wall_cut(edge, at, w + 1, -LIP_H - 1, LIP_H + 1, depth=inset + 1.5)
    for edge, a0, a1 in B["lip_clear"]:                             # nor sit on tall sockets
        out -= wall_cut(edge, (a0 + a1) / 2, a1 - a0 + 2, -LIP_H - 1, LIP_H + 1, depth=inset + 1.5)
    board_top = FLOOR + POST_H + PCB_T - BASE_H                     # in lid coordinates (lid underside is z=0)
    clamp_top = board_top + (0 if PEGS else 1.8)
    for i in B["clamps"]:                                           # clamps hold the board on its posts
        hx, hy = B["holes"][i]
        x, y = PCB_X0 + hx, PCB_Y0 + hy
        z0 = clamp_top + CLAMP_GAP
        out += cyl(x, y, z0, CLAMP_OD, TOP - z0 + 0.01)
        out -= cyl(x, y, z0 - 0.01, CLAMP_ID, PEG_H + 0.6)
    cx, cy, d, arm = L / 2, W / 2, 0.6, 13.0                        # the mark: a V
    for sx in (-1, 1):
        a = cyl(cx + sx * arm * 0.62, cy + arm * 0.62, LID_H - d, 4.4, d + 0.1)
        b = cyl(cx, cy - arm * 0.62, LID_H - d, 4.4, d + 0.1)
        out -= Manifold.batch_hull([a, b])
    # status light: a pipe from above the LED(s) to one dot in the lid
    pts = [(PCB_X0 + x, PCB_Y0 + y) for x, y in B["leds"]]
    mx, my = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    pz = board_top + B["pipe_z0"]
    h = TOP - pz + 0.01
    outer = Manifold.batch_hull([cyl(x, y, pz, 3.8, h) for x, y in pts])
    inner = Manifold.batch_hull([cyl(x, y, pz - 0.01, 2.6, h + 0.02) for x, y in pts])
    out += outer - inner
    if LIGHT == "pinhole":
        out -= cyl(mx, my, -0.5, 1.2, LID_H + 1)
    else:
        out -= cyl(mx, my, pz - 0.5, 2.6, LID_H - 0.4 - pz + 0.5)
    return out


def board_standin():
    z = FLOOR + POST_H
    bl, bw = B["size"]
    pcb = rrect(bl, bw, 3.0, PCB_T).translate((PCB_X0, PCB_Y0, z))
    for x, y, sx, sy, h in B["parts"]:
        pcb += box(PCB_X0 + x, PCB_Y0 + y, z + PCB_T, sx, sy, h)
    for hx, hy in B["holes"]:
        pcb -= cyl(PCB_X0 + hx, PCB_Y0 + hy, z - 1, 2.75, PCB_T + 2)
    return pcb


def save(m, name):
    mesh = m.to_mesh()
    tm = trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3], faces=np.asarray(mesh.tri_verts), process=True)
    tm.export(os.path.join(HERE, name))
    return tm


def build(key):
    use(key)
    b, l, k = base(), lid(), board_standin()
    lp = l.translate((0, 0, BASE_H))
    clash = {"lid/base": (lp ^ b).volume(), "lid/board": (lp ^ k).volume(), "base/board": (b ^ k).volume()}
    tb = save(b, "vigil_box_%s_base.stl" % key)
    tl = save(l.rotate((180, 0, 0)).translate((0, W, LID_H)), "vigil_box_%s_lid.stl" % key)
    save(b + l.translate((0, 0, BASE_H + 22)) + k, "vigil_box_%s_preview.stl" % key)
    print("%s (%s): closed box %.1f x %.1f x %.1f mm" % (key, B["name"], L, W, BASE_H + LID_H))
    for n, t in (("base", tb), ("lid", tl)):
        print("  %-5s watertight=%s  %.1f cm3" % (n, t.is_watertight, t.volume / 1000))
    for n, v in clash.items():
        print("  overlap %-10s %.3f mm3%s" % (n, v, "" if v < 0.01 else "   <-- CHECK"))
    return clash


if __name__ == "__main__":
    for key in (sys.argv[1:] or ["pi3", "zero2w"]):
        build(key)
