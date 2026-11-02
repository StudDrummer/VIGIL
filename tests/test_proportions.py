"""PROPORTIONS_V1 (watch-only): two people with different body proportions;
a correct name 'agrees', a wrong name is flagged, distance does not matter."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import person_hook as ph  # noqa: E402

T = [1000.0]
ph.time.time = lambda: T[0]
rng = np.random.default_rng(3)


def skel(shoulder, hip, arm, thigh, shin, scale=1.0, x=600, y=200):
    torso = 150 * scale
    k = np.zeros((17, 3))
    k[:, 2] = 0.9
    k[5] = (x - shoulder * torso / 2, y, 0.9); k[6] = (x + shoulder * torso / 2, y, 0.9)
    k[11] = (x - hip * torso / 2, y + torso, 0.9); k[12] = (x + hip * torso / 2, y + torso, 0.9)
    k[7] = (k[5][0] - 5, y + arm * torso, 0.9); k[8] = (k[6][0] + 5, y + arm * torso, 0.9)
    k[13] = (k[11][0], k[11][1] + thigh * torso, 0.9); k[14] = (k[12][0], k[12][1] + thigh * torso, 0.9)
    k[15] = (k[13][0], k[13][1] + shin * torso, 0.9); k[16] = (k[14][0], k[14][1] + shin * torso, 0.9)
    k[:, :2] += rng.normal(0, 2.5 * scale, (17, 2))
    return k


A = dict(shoulder=0.95, hip=0.62, arm=0.62, thigh=0.95, shin=0.90)   # tall adult
B = dict(shoulder=0.78, hip=0.70, arm=0.55, thigh=0.80, shin=0.72)   # different build


def feed(person, pid, tid, n, scale=1.0):
    for _ in range(n):
        T[0] += 0.25
        ph._prop_observe([skel(**person, scale=scale)], [(pid, 0)], [tid], {"a", "b"})


def test_learns_and_checks():
    feed(A, "a", 1, 60)
    feed(B, "b", 2, 60)
    T[0] += 120
    before = dict(ph._PROP_STATS)
    feed(A, "a", 3, 20, scale=0.6)            # same person, further from the camera
    assert ph._PROP_STATS["agree"] > before["agree"]
    T[0] += 120
    d0 = ph._PROP_STATS["disagree"]
    feed(B, "a", 4, 20)                        # person B wrongly named a
    assert ph._PROP_STATS["disagree"] == d0 + 1


if __name__ == "__main__":
    test_learns_and_checks()
    print("ok", ph._PROP_STATS["checks"], "checks:", {k: ph._PROP_STATS[k] for k in ("agree", "disagree", "unsure")})
