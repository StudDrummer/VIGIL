"""ENROLL_V1: Introduce everyone keeps reading a person (face, clothing from the
front, side and back, build, height, walk) and only reports done, and commits,
once every part is in."""
import json
import os
import sys
import tempfile
import types

import numpy as np

d = tempfile.mkdtemp()
os.environ["VIGIL_REID_NOLOAD"] = "1"
os.environ["VIGIL_ENROLL_REQ"] = os.path.join(d, "req.json")
os.environ["VIGIL_ENROLL_OUT"] = os.path.join(d, "out.json")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "jetson-backend"))
import person_hook as ph  # noqa: E402

rng = np.random.default_rng(1)


def kp(view):
    k = np.zeros((17, 3)); k[:, 2] = 0.9
    pts = {0: (0, -170), 1: (-8, -178), 2: (8, -178), 3: (-18, -172), 4: (18, -172),
           5: (-40, -130), 6: (40, -130), 7: (-48, -70), 8: (48, -70), 9: (-50, -15), 10: (50, -15),
           11: (-28, 0), 12: (28, 0), 13: (-30, 90), 14: (30, 90), 15: (-30, 180), 16: (30, 180)}
    for i, (x, y) in pts.items():
        k[i, :2] = (500 + x, 300 + y)
    if view == "side":
        k[5, 0], k[6, 0] = 495, 505
    if view == "back":
        k[0:3, 2] = 0.1
    return k


class Gal:
    def __init__(self): self.added = {}
    def add(self, pid, vecs, pin=False): self.added[pid] = self.added.get(pid, 0) + len(vecs)
    def save(self, force=False): pass


class V8:
    def __init__(self): self.faces = 0; self.walk = False
    def learn_face(self, pid, embs): self.faces += len(embs); return len(embs)
    def learn_walk(self, pid, sig): self.walk = True; return True
    def save(self, force=False): pass


def state():
    with open(os.environ["VIGIL_ENROLL_OUT"]) as f:
        return json.load(f)


def frame(view, t=7, walking=False, with_face=True):
    ph._EN["read_t"] = 0; ph._EN["write_t"] = 0
    if with_face:
        ph._V8_TF.setdefault(t, []).append((rng.normal(size=8), 0.8))
    ph._PROP_TRK.setdefault(t, []).append(rng.normal(size=4))
    ph._V8_TH.setdefault(t, []).append(0.5)
    ph._V8_WALK.signature = lambda tid: (1.9, 0.5) if walking else None
    ph._enroll_tick([kp(view)], [t], [ph._kp_box(kp(view))], {t: rng.normal(size=16)})


def test_learns_every_part_then_commits():
    ph._VR = types.SimpleNamespace(GALLERY=Gal())
    ph._V8 = V8()
    ph._V8_FACE = types.SimpleNamespace(ok=True)
    ph._V8_WALK = types.SimpleNamespace()
    ph._prop_learn = lambda t, p, wrong=None: None
    ph._v8_learn_track = lambda *a, **k: None
    ph._named_ids = lambda: {"p1"}
    with open(os.environ["VIGIL_ENROLL_REQ"], "w") as f:
        json.dump({"key": "k1", "person_id": "p1", "name": "Shivani", "track": 7, "t": __import__("time").time()}, f)
    for _ in range(13):
        frame("front")
    st = state()
    assert st["active"] and st["stage"] == "turn" and not st["done"], st      # front, face, build, height in
    assert 0.3 <= st["progress"] < 0.67, st
    for _ in range(6):
        frame("side")
    assert state()["stage"] == "turn"                                         # still needs the back
    ph._EN["track_hint"] = None
    ph._enroll_tick([], [], [], {})                                           # stepped out of view
    lost = (ph._EN.update(write_t=0) or ph._enroll_write() or state())
    assert lost["in_view"] is False and "Step back" in lost["prompt"], lost
    ph._track_to_pid[7] = "p1"          # the app's naming has been applied by now
    for _ in range(6):
        frame("back")
    assert state()["stage"] == "walk" and ph._VR.GALLERY.added == {}          # nothing committed early
    frame("front", walking=True)
    st = state()
    assert st["done"] and st["progress"] == 1.0, st
    assert ph._VR.GALLERY.added["p1"] >= 16 and ph._V8.faces >= 8 and ph._V8.walk
    assert any("clothing from 3 views" in x for x in st["learned"]), st
    assert ph._V8_VER[7] == "p1"


def test_stop_request_ends_it():
    with open(os.environ["VIGIL_ENROLL_REQ"], "w") as f:
        json.dump({"stop": True, "t": __import__("time").time()}, f)
    frame("front")
    assert state()["active"] is False


def test_lost_and_renamed_midway():
    """ENROLL_V2: mom is lost, comes back as a new track that identity called
    Nitin; she is the only person in view, so learning continues on her."""
    import time as _t
    with open(os.environ["VIGIL_ENROLL_REQ"], "w") as f:
        json.dump({"key": "k2", "person_id": "mom", "name": "Shivani", "track": 20, "t": _t.time()}, f)
    for _ in range(5):
        frame("front", t=20)
    assert ph._EN["track"] == 20
    ph._EN["seen_t"] = _t.time() - 2          # lost for 2 s
    ph._track_to_pid[31] = "nitin"            # new track, wrong name
    k = kp("front"); ph._EN["track"] = None; ph._EN["track_hint"] = None
    ph._EN["started"] = 0
    # the same body drawn twice must not count as someone else
    k2 = k.copy(); k2[:, :2] += 3
    ph._EN["read_t"] = 0; ph._EN["write_t"] = 0
    ph._enroll_tick([k, k2], [31, 32], [ph._kp_box(k), ph._kp_box(k2)], {31: rng.normal(size=16)})
    st = state()
    assert ph._EN["track"] == 31 and ph._track_to_pid[31] == "mom", (ph._EN["track"], ph._track_to_pid.get(31))
    assert st["others_in_view"] == 0 and st["in_view"], st
    # a real second person far away is counted, and never taken over
    far = kp("front"); far[:, 0] += 600
    ph._EN["read_t"] = 0; ph._EN["write_t"] = 0
    ph._enroll_tick([k, far], [31, 40], [ph._kp_box(k), ph._kp_box(far)], {})
    assert state()["others_in_view"] == 1 and ph._EN["track"] == 31
    # lost again with two people in view: do not guess
    ph._EN["track"] = None; ph._EN["seen_t"] = _t.time() - 2
    ph._track_to_pid.pop(31, None)
    ph._EN["read_t"] = 0; ph._EN["write_t"] = 0
    ph._enroll_tick([far, kp("front")], [40, 41], [ph._kp_box(far), ph._kp_box(kp("front"))], {})
    assert ph._EN["track"] is None and state()["in_view"] is False


if __name__ == "__main__":
    test_learns_every_part_then_commits(); test_stop_request_ends_it(); test_lost_and_renamed_midway(); print("ok")
