"""PERSON_ACTIVITY_V1: a walking person counts as active, a seated still one as
still, and the totals are posted once a minute."""
import os
import sys

import numpy as np

os.environ["VIGIL_REID_NOLOAD"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import person_hook as ph  # noqa: E402

T = [1000.0]
ph.time.time = lambda: T[0]
sent = []
ph.requests.post = lambda url, json=None, **k: sent.append(json)
rng = np.random.default_rng(1)


def body(x, y=300.0):
    k = np.zeros((17, 3)); k[:, 2] = 0.9
    k[5] = (x - 40, y, 0.9); k[6] = (x + 40, y, 0.9); k[11] = (x - 30, y + 150, 0.9); k[12] = (x + 30, y + 150, 0.9)
    k[:, :2] += rng.normal(0, 2.0, (17, 2))
    return k


def test_walking_vs_still():
    ph._ACT["acc"].clear(); ph._ACT["pos"].clear(); ph._ACT["sent"] = T[0]
    for i in range(8 * 70):                          # 70 s at 8 frames a second
        T[0] += 0.125
        ph._activity_observe([body(200 + i * 6.0), body(900.0)], [("walker", 0), ("sitter", 0)], {"walker", "sitter"})
    assert sent, "nothing posted"
    got = {it["person_id"]: it for it in sent[-1]["items"]}
    assert got["walker"]["active_s"] > 50 and got["walker"]["still_s"] < 5, got
    assert got["sitter"]["still_s"] > 50 and got["sitter"]["active_s"] < 5, got


if __name__ == "__main__":
    test_walking_vs_still()
    print("ok", sent[-1])
