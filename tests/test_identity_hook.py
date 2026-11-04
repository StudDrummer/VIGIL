"""IDENTITY_V8 inside person_hook: a correction teaches every signal, a face
names someone in new clothes, a face moves a wrong name, a known face never
becomes a duplicate person, and the whole frame loop runs without errors."""
import os
import sys
import tempfile
from collections import deque

import numpy as np
import pytest

TMP = tempfile.mkdtemp()
os.environ["VIGIL_REID_NOLOAD"] = "1"
os.environ["VIGIL_IDENTITY_DIR"] = TMP
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import person_hook as ph  # noqa: E402

VI = ph._VI
R = ph._VR
T = [5000.0]
rng = np.random.default_rng(3)
FC = {p: (lambda v: v / np.linalg.norm(v))(rng.normal(size=128)) for p in "AB"}
AC = {p: rng.normal(size=384) for p in ("A", "B", "Bnew")}


def face(p, n):
    x = FC[p] + rng.normal(0, 0.066, (n, 128))
    return (x / np.linalg.norm(x, axis=1, keepdims=True)).astype(np.float32)


def look(c, n):
    x = AC[c] + rng.normal(0, 0.6, (n, 384))
    return (x / np.linalg.norm(x, axis=1, keepdims=True)).astype(np.float32)


@pytest.fixture(autouse=True)
def _isolate():
    """Other test files share person_hook: leave nothing behind."""
    real_time = ph.time.time
    ph.time.time = lambda: T[0]
    yield
    ph.time.time = real_time
    m = VI.IdentityModel()
    for store in (m.fvec, m.fpin, m.trk_face, m.trk_gait, m.gait_ver, m.gait_auto):
        store.clear()
    VI.MODEL = m
    ph._track_to_pid.clear(); ph._signatures.clear()
    ph._named_cache[0] = 0.0
    ph._mode["t"] = 0.0


def fresh():
    VI.MODEL = VI.IdentityModel()
    VI.MODEL._face_on, VI.MODEL._face_t = True, 1e18
    R.EMBEDDER.kind = "dinov2"
    R.GALLERY = R.Gallery(path=os.path.join(tempfile.mkdtemp(), "g.npz"))
    R.GALLERY.named = {"A", "B"}
    R.GALLERY.add("A", look("A", 20), pin=True)
    R.GALLERY.add("B", look("B", 20), pin=True)
    for k in ("check", "votes", "bs", "p"):
        ph._V8[k].clear()
    ph._trk_fp.clear(); ph._trk_fp_t.clear(); ph._track_to_pid.clear()
    ph._signatures.clear()
    for p in "AB":
        ph._signatures[p] = ph._PersonSignature(p)
    ph._named_cache[0], ph._named_cache[1] = 1e18, {"A", "B"}
    ph._mode.update({"mode": "family", "t": 1e18, "resident": None})


def track(tid, face_of, look_of, nf=4):
    VI.MODEL.trk_face[tid] = deque([(T[0], v, 0.8) for v in face(face_of, nf)], maxlen=12)
    for v in look(look_of, 6):
        T[0] += 0.1
        ph._trk_fp.setdefault(tid, []).append(v)
        ph._trk_fp_t.setdefault(tid, []).append(T[0])


def test_correction_teaches_faces_and_calibration():
    fresh()
    for i in range(14):                                     # two weeks of taps in the app
        p = "AB"[i % 2]
        tid = 100 + i
        track(tid, p, p)
        ph._v8_learn(tid, p)
    assert len(VI.MODEL.fvec["A"]) > 0 and VI.MODEL.fpin["A"].all()
    # the first tap has no face memory to compare with yet; after that every tap is an example
    assert VI.MODEL.cal["face"].counts() == (12, 13)
    assert VI.MODEL.cal["face"].learned and VI.MODEL.cal["face"].auc > 0.95
    assert os.path.exists(os.path.join(TMP, "identity_report.json"))


def test_face_names_person_in_new_clothes():
    fresh()
    VI.MODEL._face_add("A", face("A", 6), pin=True)
    VI.MODEL._face_add("B", face("B", 6), pin=True)
    track(7, "B", "Bnew")                                   # B in clothes never seen before
    ev = {"c": [], "h": [], "n": 3}
    assert ph._v8_early(7, ev, set())
    who, why = ev["early"]
    assert who == "B" and why.startswith("fusion strong"), why


def test_old_name_held_back_or_fixed_by_face():
    fresh()
    VI.MODEL._face_add("A", face("A", 6), pin=True)
    VI.MODEL._face_add("B", face("B", 6), pin=True)
    track(8, "A", "B")                                      # A wearing B's outfit
    who, why = ph._v8_decide(8, set(), ("B", "fingerprint lead"))
    assert who == "A", why


def test_face_moves_a_wrong_name():
    fresh()
    VI.MODEL._face_add("A", face("A", 6), pin=True)
    VI.MODEL._face_add("B", face("B", 6), pin=True)
    ph._track_to_pid[9] = "A"
    out = [("A", 100)]
    for _ in range(2):                                      # two checks in a row
        T[0] += 2.1
        track(9, "B", "B")
        out = ph._v8_verify(out, [9])
    assert out == [("B", 100)] and ph._track_to_pid[9] == "B"


def test_known_face_is_never_a_new_person():
    fresh()
    VI.MODEL._face_add("A", face("A", 6), pin=True)
    track(10, "A", "Bnew")
    assert ph._v8_new_ok(10) is False
    VI.MODEL.trk_face.pop(10)
    assert ph._v8_new_ok(10) is True                        # no face seen: the old rules decide


def test_frame_loop_runs_clean(capsys):
    fresh()
    frame = np.full((720, 1280, 3), 90, np.uint8)
    for i in range(40):
        T[0] += 0.12
        ph_ = 2 * np.pi * 0.9 * T[0]
        persons = []
        for x0 in (300, 800):
            x = x0 + 30 * i * 0.12
            kp = np.zeros((17, 3)); kp[:, 2] = 0.9
            kp[0] = [x, 120, .9]; kp[1] = [x - 6, 112, .9]; kp[2] = [x + 6, 112, .9]
            kp[3] = [x - 14, 116, .9]; kp[4] = [x + 14, 116, .9]
            kp[5] = [x - 30, 170, .9]; kp[6] = [x + 30, 170, .9]
            kp[7] = [x - 34, 240, .9]; kp[8] = [x + 34, 240, .9]
            kp[9] = [x - 36 - 10 * np.sin(ph_), 300, .9]; kp[10] = [x + 36 + 10 * np.sin(ph_), 300, .9]
            kp[11] = [x - 22, 330, .9]; kp[12] = [x + 22, 330, .9]
            kp[13] = [x - 22, 450, .9]; kp[14] = [x + 22, 450, .9]
            kp[15] = [x + 40 * np.sin(ph_), 570, .9]; kp[16] = [x - 40 * np.sin(ph_), 570, .9]
            persons.append(kp)
        ph.vigil_track_persons(persons, frame=frame, track_ids=[1, 2])
    err = capsys.readouterr().out
    assert "error" not in err.lower(), err[-2000:]


def test_face_vetoes_a_clothing_swap():
    fresh()
    VI.MODEL._face_add("A", face("A", 6), pin=True)
    VI.MODEL._face_add("B", face("B", 6), pin=True)
    ph._track_to_pid.update({1: "A", 2: "B"})
    out = [("A", 0), ("B", 0)]
    for _ in range(3):
        T[0] += 2.1
        track(1, "A", "B"); track(2, "B", "A")              # they swapped clothes; faces are right
        out = ph._v7_verify(out, [1, 2])
    assert out == [("A", 0), ("B", 0)], out
