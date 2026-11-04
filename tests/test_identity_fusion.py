"""IDENTITY_V8: face, appearance, proportions, height and gait vote together,
and each signal's weight is learned from confirmations and corrections.

Simulated household of three. The hard cases are the ones that broke the old
logic: a person in clothes VIGIL has never seen, and someone wearing clothes
that look like another person's."""
import os
import sys
import tempfile
from collections import deque

import numpy as np

TMP = tempfile.mkdtemp()
os.environ["VIGIL_REID_NOLOAD"] = "1"
os.environ["VIGIL_IDENTITY_DIR"] = TMP
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import vigil_identity as VI  # noqa: E402

rng = np.random.default_rng(7)
PEOPLE = ["A", "B", "C"]
CENTER = {p: (lambda v: v / np.linalg.norm(v))(rng.normal(size=128)) for p in PEOPLE}
HEIGHT = {"A": 0.0, "B": 8.0, "C": 10.0}           # true height, percent; B and C are close


def faces(p, n=4, sigma=0.066):
    x = CENTER[p] + rng.normal(0, sigma, (n, 128))
    return (x / np.linalg.norm(x, axis=1, keepdims=True)).astype(np.float32)


def other_scores(true, cands, outfit="usual"):
    """Scores for the signals person_hook owns. `outfit`: usual, new (never
    seen), or lookalike (dressed like the next person)."""
    out = {}
    look_like = PEOPLE[(PEOPLE.index(true) + 1) % 3]
    for c in cands:
        if outfit == "usual":
            app = rng.normal(0.82, 0.05) if c == true else rng.normal(0.55, 0.08)
        elif outfit == "new":
            app = rng.normal(0.58, 0.06)
        else:
            app = rng.normal(0.84, 0.04) if c == look_like else rng.normal(0.6, 0.06)
        prop = -abs(rng.normal(1.0, 0.4)) if c == true else -abs(rng.normal(2.6, 0.9))
        hgt = -abs(HEIGHT[true] - HEIGHT[c] + rng.normal(0, 1.5))
        out[c] = {"appearance": app, "proportions": prop, "height": hgt}
    return out


def fresh_model():
    m = VI.IdentityModel()
    m._face_on, m._face_t = True, 1e18
    for k in ("named_by_fusion", "held_back", "overruled", "face_swaps", "new_blocked", "learned_events"):
        m.stats[k] = 0
    return m


def put_track(m, tid, p, nfaces=4, gait=True):
    m.trk_face[tid] = deque([(VI.time.time(), v, 0.8) for v in faces(p, nfaces)], maxlen=12)
    if gait:   # gait here is uninformative on purpose: same distribution for everyone
        m.trk_gait[tid] = (VI.time.time(), np.array([1.8, 0.6, 0.4, 0.05]) + rng.normal(0, [0.1, 0.06, 0.08, 0.02]))


def scores_for(m, tid, true, outfit, cands=PEOPLE):
    own, q = m.own_scores(tid, cands)
    o = other_scores(true, cands, outfit)
    for c in cands:
        o[c].update(own.get(c, {}))
    return o, q


def teach(m, n_events=30):
    """A family confirming and correcting names for a few days."""
    for i in range(n_events):
        p = PEOPLE[i % 3]
        tid = 1000 + i
        put_track(m, tid, p)
        others = [c for c in PEOPLE if c != p]
        o = other_scores(p, PEOPLE, outfit=["usual", "new", "lookalike"][i % 3])
        m.learn(tid, p, others, o)


def test_calibration_learns_a_strong_signal_and_ignores_a_useless_one():
    c = VI.Calibration("face")
    for _ in range(80):
        c.pairs.append((float(rng.normal(0.62, 0.08)), 1))
        c.pairs.append((float(rng.normal(0.05, 0.1)), 0))
    c.fit()
    assert c.learned and c.auc > 0.98
    assert c.vote(0.62) >= 4 and c.vote(0.05) <= -4

    g = VI.Calibration("gait")
    for _ in range(80):
        g.pairs.append((float(-abs(rng.normal(1.5, 0.8))), 1))
        g.pairs.append((float(-abs(rng.normal(1.5, 0.8))), 0))
    g.fit()
    assert g.learned and abs(g.vote(-0.5)) < 0.6 and abs(g.vote(-3.0)) < 0.6, (g.a, g.b)


def test_starting_values_are_capped():
    m = fresh_model()
    r = m.fuse({"A": {"height": 0.0, "gait": 0.0, "proportions": 0.0}, "B": {"height": -8.0}})
    assert not r["ok"]                                  # no face or appearance: never names anyone
    assert r["parts"]["A"]["height"] <= VI.CAP["height"][1]


def test_face_names_a_person_in_new_clothes():
    m = fresh_model()
    for p in PEOPLE:
        m._face_add(p, faces(p, 6), pin=True)
    put_track(m, 1, "B")
    s, q = scores_for(m, 1, "B", "new")
    r = m.fuse(s, q)
    assert r["ok"] and r["best"] == "B", m.describe(r)


def test_lookalike_clothes_do_not_fool_it():
    m = fresh_model()
    for p in PEOPLE:
        m._face_add(p, faces(p, 6), pin=True)
    put_track(m, 2, "A")                                # A dressed like B
    s, q = scores_for(m, 2, "A", "lookalike")
    r = m.fuse(s, q)
    assert r["best"] == "A", m.describe(r)


def test_learning_beats_appearance_alone():
    m = fresh_model()
    teach(m, 36)
    for s in ("face", "appearance", "proportions", "height"):
        assert m.cal[s].learned, s
    assert m.cal["face"].auc > 0.95
    assert m.cal["gait"].a < 0.35 * VI.PRIOR["gait"][0], m.cal["gait"].status()    # learned to distrust it
    right_fused = right_app = named = wrong_named = 0
    trials = 0
    for i in range(90):
        p = PEOPLE[i % 3]
        outfit = ["usual", "new", "lookalike"][(i // 3) % 3]
        tid = 5000 + i
        put_track(m, tid, p, nfaces=3)
        s, q = scores_for(m, tid, p, outfit)
        r = m.fuse(s, q)
        trials += 1
        right_fused += r["best"] == p
        named += r["ok"]
        wrong_named += r["ok"] and r["best"] != p
        right_app += max(PEOPLE, key=lambda c: s[c]["appearance"]) == p
    assert right_fused / trials >= 0.97, right_fused / trials
    assert right_app / trials <= 0.75                   # the old way, on the same hard cases
    assert named / trials >= 0.9 and wrong_named == 0, (named, wrong_named)


def test_without_a_face_it_still_holds_back_rather_than_guessing():
    m = fresh_model()
    teach(m, 36)
    held = wrong = 0
    for i in range(30):
        p = PEOPLE[i % 3]
        tid = 7000 + i
        m.trk_gait[tid] = (VI.time.time(), np.array([1.8, 0.6, 0.4, 0.05]))
        s = other_scores(p, PEOPLE, "lookalike")      # back to the camera, dressed like someone else
        r = m.fuse(s, {})
        held += not r["ok"]
        wrong += r["ok"] and r["best"] != p
    assert wrong == 0, (held, wrong)                   # it may name from body shape, but never wrongly


def test_correction_removes_wrongly_learned_faces():
    m = fresh_model()
    m._face_add("A", faces("A", 5), pin=True)
    m._face_add("B", faces("B", 5), pin=True)
    m._face_add("A", faces("B", 3), pin=False)           # B's face wrongly learned as A
    assert m.face_score(faces("B", 2), "A") > 0.5
    put_track(m, 9, "B")
    m.learn(9, "B", ["A"], other_scores("B", ["A", "B"]), wrong="A")
    assert m.face_score(faces("B", 2), "A") < 0.36


def test_auto_learn_only_keeps_faces_that_are_clearly_theirs():
    m = fresh_model()
    m._face_add("A", faces("A", 5), pin=True)
    m._face_add("B", faces("B", 5), pin=True)
    put_track(m, 11, "B")
    n = len(m.fvec["A"])
    m.auto_learn(11, "A", 0.99)                          # wrongly named A: B's face must not go in
    assert len(m.fvec["A"]) == n


def test_save_load_and_face_off_erases():
    m = fresh_model()
    teach(m, 24)
    m.save(force=True)
    m2 = fresh_model()
    assert set(m2.fvec) == set(PEOPLE) and m2.cal["face"].learned
    with open(VI.SETTINGS_JSON, "w") as f:
        f.write('{"face": false}')
    m2._face_t = 0
    assert m2.face_on() is False and not m2.fvec and not os.path.exists(VI.STATE_NPZ)
    os.remove(VI.SETTINGS_JSON)


def test_gait_signature_measures_cadence():
    g = VI.Gait()
    t0 = 100.0
    for i in range(60):                                   # 7.5 s at 8 fps, 1.8 steps/s
        t = t0 + i / 8.0
        ph = 2 * np.pi * 0.9 * t
        x = 200 + 40 * (t - t0)
        kp = np.zeros((17, 3))
        kp[:, 2] = 0.9
        kp[5] = [x - 20, 100, .9]; kp[6] = [x + 20, 100, .9]
        kp[11] = [x - 15, 200, .9]; kp[12] = [x + 15, 200, .9]
        kp[13] = [x - 15 + 15 * np.sin(ph), 280, .9]; kp[14] = [x + 15 - 15 * np.sin(ph), 280, .9]
        kp[15] = [x + 35 * np.sin(ph), 360, .9]; kp[16] = [x - 35 * np.sin(ph), 360, .9]
        kp[9] = [x - 25 - 20 * np.sin(ph), 210, .9]; kp[10] = [x + 25 + 20 * np.sin(ph), 210, .9]
        g.add(1, kp, t)
    sig = g.signature(1)
    assert sig is not None and abs(sig[0] - 1.8) < 0.25, sig


def test_face_model_is_safe_without_a_face():
    m = fresh_model()
    kp = np.zeros((17, 3))
    kp[0] = [320, 200, .9]; kp[1] = [310, 190, .9]; kp[2] = [330, 190, .9]
    blank = np.full((480, 640, 3), 120, np.uint8)
    assert m.face.embed(blank, kp) is None               # no face there: no signature, no crash
    assert VI.FaceModel.head_box((480, 640), np.zeros((17, 3))) is None   # face turned away


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
