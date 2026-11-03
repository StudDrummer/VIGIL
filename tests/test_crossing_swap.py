"""IDENTITY_V7: when two people cross and the tracker swaps their track
numbers, the names are swapped back once they separate; with correct names
nothing changes; nothing is learned while they overlap."""
import os
import sys
import tempfile

import numpy as np

os.environ["VIGIL_REID_NOLOAD"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import person_hook as ph  # noqa: E402

T = [1000.0]
ph.time.time = lambda: T[0]
R = ph._VR
R.EMBEDDER.kind = "dinov2"
rng = np.random.default_rng(5)
d = 384
cA, cB = rng.normal(size=d), rng.normal(size=d)


def look(c, n):
    x = c + rng.normal(0, 0.6, (n, d))
    return (x / np.linalg.norm(x, axis=1, keepdims=True)).astype(np.float32)


def fresh():
    R.GALLERY = R.Gallery(path=os.path.join(tempfile.mkdtemp(), "g.npz"))
    R.GALLERY.named = {"A", "B"}
    R.GALLERY.add("A", look(cA, 20), pin=True)
    R.GALLERY.add("B", look(cB, 20), pin=True)
    for k in ("pending", "last_cross", "votes"):
        ph._V7[k].clear()
    ph._V7["last_check"] = 0.0
    ph._trk_fp.clear(); ph._trk_fp_t.clear(); ph._track_to_pid.clear()


def feed(tid, c, n=4):
    for v in look(c, n):
        T[0] += 0.1
        ph._trk_fp.setdefault(tid, []).append(v)
        ph._trk_fp_t.setdefault(tid, []).append(T[0])


def test_swap_after_crossing_is_fixed():
    fresh()
    ph._track_to_pid.update({1: "B", 2: "A"})            # tracker swapped them while crossing
    ph._v7_mark_crossings([(0, 0, 100, 300), (50, 0, 150, 300)], [1, 2])
    assert ph._v7_hold(1) and ph._v7_hold(2)              # no learning while crossing
    T[0] += 0.6
    feed(1, cA); feed(2, cB)                              # after separating: 1 is really A
    out = ph._v7_verify([("B", 0), ("A", 0)], [1, 2])
    assert out == [("A", 0), ("B", 0)], out
    assert ph._track_to_pid[1] == "A" and ph._track_to_pid[2] == "B"


def test_correct_names_unchanged():
    fresh()
    ph._track_to_pid.update({1: "A", 2: "B"})
    for _ in range(5):
        T[0] += 2.1
        feed(1, cA); feed(2, cB)
        out = ph._v7_verify([("A", 0), ("B", 0)], [1, 2])
        assert out == [("A", 0), ("B", 0)]


def test_slow_mixup_needs_two_checks():
    fresh()
    ph._track_to_pid.update({1: "B", 2: "A"})             # wrong, no crossing seen
    T[0] += 2.1; feed(1, cA); feed(2, cB)
    assert ph._v7_verify([("B", 0), ("A", 0)], [1, 2]) == [("B", 0), ("A", 0)]
    T[0] += 2.1; feed(1, cA); feed(2, cB)
    assert ph._v7_verify([("B", 0), ("A", 0)], [1, 2]) == [("A", 0), ("B", 0)]


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
