"""FALL_LAB_V1: synthetic stick-figure sequences for testing fall logic.

A 2-D body (COCO 17 keypoints) seen roughly from the side, posed and moved
between postures. These are NOT a substitute for real recordings: they check
that the logic does what it says on clear-cut cases (fast fall, slow slump,
lying down on a bed, sitting, bending, crouching) at different frame rates.
"""
import numpy as np

H = 600.0                      # body height in pixels
SHAPE = (1296, 2304)           # Tapo C210 frame (h, w)
GROUND = 1150.0


def _pose(name, x0=900.0, d=1.0):
    """17x2 keypoints for a named posture. d = facing direction (+1 right)."""
    g = GROUND
    def P(*pts):
        return np.array(pts, dtype=float)
    if name == "stand":
        hip, sh, head, knee, ank = g - .53 * H, g - .82 * H, g - .93 * H, g - .28 * H, g - .02 * H
        return P((x0, head), (x0 - 8, head - 6), (x0 + 8, head - 6), (x0 - 18, head), (x0 + 18, head),
                 (x0 - 45, sh), (x0 + 45, sh), (x0 - 50, sh + .17 * H), (x0 + 50, sh + .17 * H),
                 (x0 - 50, sh + .32 * H), (x0 + 50, sh + .32 * H), (x0 - 25, hip), (x0 + 25, hip),
                 (x0 - 25, knee), (x0 + 25, knee), (x0 - 25, ank), (x0 + 25, ank))
    if name == "sit_chair":                       # seat at knee height, shins hang
        hip, sh = g - .27 * H, g - .56 * H
        kx = x0 + d * .25 * H
        head = sh - .11 * H
        return P((x0, head), (x0 - 8, head - 6), (x0 + 8, head - 6), (x0 - 18, head), (x0 + 18, head),
                 (x0 - 45, sh), (x0 + 45, sh), (x0 - 45, sh + .17 * H), (x0 + 45, sh + .17 * H),
                 (kx - 30, hip - 10), (kx + 10, hip - 10), (x0 - 20, hip), (x0 + 20, hip),
                 (kx - 15, hip), (kx + 15, hip), (kx - 15, g - .02 * H), (kx + 15, g - .02 * H))
    if name == "sit_floor":                       # on the floor, legs out in front
        hip, sh = g - .05 * H, g - .34 * H
        head = sh - .11 * H
        return P((x0, head), (x0 - 8, head - 6), (x0 + 8, head - 6), (x0 - 18, head), (x0 + 18, head),
                 (x0 - 40, sh), (x0 + 40, sh), (x0 - 35, sh + .15 * H), (x0 + 35, sh + .15 * H),
                 (x0 - 25, hip - 20), (x0 + 25, hip - 20), (x0 - 20, hip), (x0 + 20, hip),
                 (x0 + d * .25 * H, g - .06 * H), (x0 + d * .25 * H + 10, g - .06 * H),
                 (x0 + d * .48 * H, g - .03 * H), (x0 + d * .48 * H + 10, g - .03 * H))
    if name in ("lie", "lie_bed"):                # flat on the side, head toward -d
        gy = g - (.45 * H if name == "lie_bed" else .05 * H)
        hx = x0 - d * .40 * H
        def at(f, dy=0.0):
            return (x0 + d * f * H, gy - dy)
        return P(at(-.43, 30), at(-.45, 36), at(-.41, 36), at(-.47, 28), at(-.39, 28),
                 at(-.30, 40), at(-.30, 10), at(-.15, 45), at(-.15, 5), at(-.02, 45), at(-.02, 5),
                 at(-.02, 35), at(-.02, 12), at(.22, 35), at(.22, 12), at(.45, 33), at(.45, 10))
    if name == "kneel":
        hip, sh = g - .27 * H, g - .56 * H
        head = sh - .11 * H
        return P((x0, head), (x0 - 8, head - 6), (x0 + 8, head - 6), (x0 - 18, head), (x0 + 18, head),
                 (x0 - 45, sh), (x0 + 45, sh), (x0 - 48, sh + .17 * H), (x0 + 48, sh + .17 * H),
                 (x0 - 45, sh + .31 * H), (x0 + 45, sh + .31 * H), (x0 - 22, hip), (x0 + 22, hip),
                 (x0 - 22, g - .03 * H), (x0 + 22, g - .03 * H), (x0 - d * .2 * H, g - .03 * H),
                 (x0 - d * .2 * H + 12, g - .03 * H))
    if name == "crouch":
        hip, sh = g - .28 * H, g - .52 * H
        head = sh - .1 * H
        kx = x0 + d * .15 * H
        return P((x0 + d * 30, head), (x0 + d * 30 - 8, head - 6), (x0 + d * 30 + 8, head - 6), (x0 + d * 30 - 18, head),
                 (x0 + d * 30 + 18, head), (x0 - 35 + d * 20, sh), (x0 + 35 + d * 20, sh), (kx - 20, sh + .15 * H),
                 (kx + 20, sh + .15 * H), (kx - 10, g - .2 * H), (kx + 10, g - .2 * H), (x0 - 22, hip), (x0 + 22, hip),
                 (kx - 15, g - .33 * H), (kx + 15, g - .33 * H), (x0 - 15, g - .02 * H), (x0 + 15, g - .02 * H))
    if name == "bend":                            # touching the floor, legs straight
        hip = g - .53 * H
        sx = x0 + d * .30 * H
        return P((sx + d * .1 * H, hip + .05 * H), (sx + d * .1 * H, hip), (sx + d * .1 * H, hip + 10), (sx + d * .08 * H, hip),
                 (sx + d * .08 * H, hip + 10), (sx, hip - 15), (sx, hip + 25), (sx, hip + .17 * H), (sx + 10, hip + .17 * H),
                 (sx + 5, g - .08 * H), (sx + 15, g - .08 * H), (x0 - 20, hip), (x0 + 20, hip),
                 (x0 - 20, g - .28 * H), (x0 + 20, g - .28 * H), (x0 - 20, g - .02 * H), (x0 + 20, g - .02 * H))
    if name == "lie_toward":                      # lying with the feet toward the camera
        def q(dx, fy):
            return (x0 + dx, g - fy * H)
        return P(q(0, .24), q(-6, .25), q(6, .25), q(-14, .24), q(14, .24), q(-40, .19), q(40, .19),
                 q(-55, .16), q(55, .16), q(-50, .12), q(50, .12), q(-22, .11), q(22, .11),
                 q(-25, .06), q(25, .06), q(-25, .02), q(25, .02))
    raise ValueError(name)


def sequence(steps, fps=5.0, seed=0, noise=4.0, x0=900.0, walk=0.0, d=1.0, drop_conf=None):
    """steps: [(posture, seconds_to_reach_it), ...]. Yields (t, kp 17x3)."""
    rng = np.random.default_rng(seed)
    t = 0.0
    prev = _pose(steps[0][0], x0, d)
    out = []
    for name, secs in steps:
        tgt = _pose(name, x0, d)
        n = max(1, int(round(secs * fps)))
        for i in range(n):
            a = (i + 1) / n
            a = a * a * (3 - 2 * a)                # ease in/out
            xy = prev * (1 - a) + tgt * a
            xy = xy + np.array([walk * t, 0.0])
            k = np.zeros((17, 3))
            k[:, :2] = xy + rng.normal(0, noise, (17, 2))
            k[:, 2] = 0.9
            if drop_conf:
                k[drop_conf, 2] = 0.1
            out.append((t, k))
            t += 1.0 / fps
        prev = tgt
    return out


# (name, is_fall, steps). Each ends with time to settle.
SCENARIOS = [
    ("walk", False, [("stand", 1)] * 12),
    ("sit down quickly in a chair", False, [("stand", 2), ("sit_chair", 0.8), ("sit_chair", 12)]),
    ("bend to pick something up", False, [("stand", 2), ("bend", 1.0), ("bend", 2), ("stand", 1), ("stand", 5)]),
    ("crouch and hold 10 s", False, [("stand", 2), ("crouch", 1.0), ("crouch", 10), ("stand", 1), ("stand", 4)]),
    ("lie down slowly on the bed", False, [("stand", 2), ("sit_chair", 2), ("lie_bed", 3), ("lie_bed", 15)]),
    ("kneel on the floor to look under a bed", False, [("stand", 2), ("kneel", 1.5), ("kneel", 10), ("stand", 2)]),
    ("sit on the floor on purpose (slowly)", False, [("stand", 2), ("kneel", 3), ("sit_floor", 3), ("sit_floor", 15)]),
    ("FALL forward onto the floor", True, [("stand", 2), ("lie", 0.8), ("lie", 12)]),
    ("FALL sideways from kneeling", True, [("stand", 2), ("kneel", 2), ("lie", 0.6), ("lie", 12)]),
    ("FALL slump from chair to the floor", True, [("stand", 2), ("sit_chair", 2), ("sit_chair", 3), ("sit_floor", 0.8), ("sit_floor", 25)]),
    ("FALL slow collapse (3.5 s) and stays down", True, [("stand", 2), ("crouch", 1.5), ("lie", 2.0), ("lie", 20)]),
    ("FALL from chair, ends lying", True, [("stand", 2), ("sit_chair", 2), ("sit_chair", 3), ("lie", 1.0), ("lie", 12)]),
    ("FALL toward the camera (lying end-on)", True, [("stand", 2), ("lie_toward", 0.9), ("lie_toward", 12)]),
    ("sit on the floor on purpose from standing (brisk 2 s)", False, [("stand", 2), ("crouch", 1.2), ("sit_floor", 1.6), ("sit_floor", 25)]),
    ("sit on the floor on purpose from a chair (slow)", False, [("stand", 2), ("sit_chair", 2), ("sit_chair", 3), ("sit_floor", 3.5), ("sit_floor", 25)]),
]
BED_ZONE = [{"x1": 0.25, "y1": 0.55, "x2": 0.75, "y2": 0.76}]   # where lie_bed lands (normalized)


def perspective_walk(fps=5.0, seconds=14.0, toward=False, seed=0):
    """Walking straight away from (or toward) the camera: the body shrinks or grows
    in the picture and the feet move up or down it. Nothing is a fall."""
    rng = np.random.default_rng(seed)
    base = _pose("stand", 1150.0)
    out = []
    n = int(seconds * fps)
    for i in range(n):
        t = i / fps
        a = t / seconds
        sc = (1.25 - 0.9 * a) if not toward else (0.35 + 0.9 * a)      # 1.25 -> 0.35 (away)
        foot_y = (1250 - 650 * a) if not toward else (600 + 650 * a)
        xy = base.copy()
        xy[:, 0] = 1150 + (xy[:, 0] - 1150) * sc
        xy[:, 1] = foot_y - (GROUND - xy[:, 1]) * sc
        step = np.sin(2 * np.pi * 0.9 * t) * 18 * sc               # legs swing
        xy[15, 1] -= max(0, step); xy[16, 1] -= max(0, -step)
        k = np.zeros((17, 3)); k[:, :2] = xy + rng.normal(0, 3, (17, 2)); k[:, 2] = 0.9
        out.append((t, k))
    return out
