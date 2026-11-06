"""IDENTITY_V8: fused identity from every cue, learning from corrections, one
name per person in view, numbered "Not sure" labels."""
import os
import sys
import tempfile

import numpy as np

os.environ["VIGIL_REID_NOLOAD"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "jetson-backend"))
import vigil_identity as vi  # noqa: E402
import person_hook as ph  # noqa: E402

rng = np.random.default_rng(3)


def body(x, y=300.0, s=1.0):
    k = np.zeros((17, 3))
    k[:, 2] = 0.9
    pts = {0: (0, -170), 1: (-8, -178), 2: (8, -178), 3: (-18, -172), 4: (18, -172),
           5: (-40, -130), 6: (40, -130), 7: (-48, -70), 8: (48, -70), 9: (-50, -15), 10: (50, -15),
           11: (-28, 0), 12: (28, 0), 13: (-30, 90), 14: (30, 90), 15: (-30, 180), 16: (30, 180)}
    for i, (dx, dy) in pts.items():
        k[i, :2] = (x + dx * s, y + dy * s)
    return k


def test_fusion_learns_which_cues_work():
    f = vi.Fusion(path=os.path.join(tempfile.mkdtemp(), "v8.json"))
    # before learning: a strong face match is strong evidence, a poor one is against
    assert f.evidence({"face": 0.6})[0] > 2.0
    # VALIDATED_V1: before this house has shown the face cue works, it cannot vote against
    assert f.evidence({"face": 0.05})[0] == 0.0
    for _ in range(30):
        f.add_example("face", rng.normal(0.55, 0.1), True)
        f.add_example("face", rng.normal(0.08, 0.08), False)
    assert f.validated("face") and f.evidence({"face": 0.05})[0] < -2.0
    # this house: height does not separate people (everyone the same height) ...
    for _ in range(80):
        f.add_example("height", -abs(rng.normal(0, 0.03)), True)
        f.add_example("height", -abs(rng.normal(0, 0.03)), False)
        # ... but body proportions separate them well
        f.add_example("body", rng.normal(-0.9, 0.4), True)
        f.add_example("body", rng.normal(-3.5, 0.8), False)
    rep = f.report()
    assert rep["height"]["weight"] <= 0.3, rep["height"]
    assert rep["body"]["weight"] >= 0.8, rep["body"]
    tot_same, _ = f.evidence({"body": -0.8, "height": -0.01})
    tot_diff, _ = f.evidence({"body": -3.8, "height": -0.01})
    assert tot_same > 1.5 and tot_diff < -1.5, (tot_same, tot_diff)
    f.save(force=True)
    g = vi.Fusion(path=f.path)
    assert len(g.stats["body"]["same"]) == 80


def test_walk_signature():
    w = vi.WalkTracker()
    t0 = 1000.0
    for i in range(8 * 4):                       # 4 s at 8 frames a second, walking right
        t = t0 + i / 8.0
        k = body(100 + i * 12.0)
        ph_ = 2 * np.pi * 0.95 * (t - t0)        # 0.95 strides a second = 1.9 steps a second
        k[15, 0] += 35 * np.sin(ph_)
        k[16, 0] -= 35 * np.sin(ph_)
        k[9, 0] -= 20 * np.sin(ph_)
        k[10, 0] += 20 * np.sin(ph_)
        w.observe(7, k, t)
    sig = w.signature(7)
    assert sig is not None
    assert 1.5 <= sig[0] <= 2.3, sig
    standing = vi.WalkTracker()
    for i in range(40):
        standing.observe(8, body(300.0), t0 + i / 8.0)
    assert standing.signature(8) is None


def _fresh_hook():
    ph._trk_status.clear()
    ph._track_to_pid.clear()
    ph._NS_NUM.clear()
    ph._V8_VER.clear()


def test_one_name_per_person_and_numbering():
    _fresh_hook()
    ph._v8_raw = lambda t, p: {"appearance": 0.9} if t == 1 else {"appearance": 0.4}
    people = [body(200), body(600), body(1000)]
    # the same name ended up on two people: track 1 fits better and keeps it
    out, labels = ph._v8_finalize([("person_1", 300), ("person_1", 290), (None, None)], [1, 2, 3], people)
    assert out[0][0] == "person_1" and out[1][0] is None, out
    assert ph._trk_status.get(2) == "Not sure"
    # two Not sure people are numbered; the numbers stay with the person
    ph._trk_status[3] = "Not sure"
    out, labels = ph._v8_finalize(out, [1, 2, 3], people)
    assert labels[1:] == ["Not sure 1", "Not sure 2"], labels
    out2, labels2 = ph._v8_finalize([out[2], out[0], out[1]], [3, 1, 2], [people[2], people[0], people[1]])
    assert labels2[0] == "Not sure 2" and labels2[2] == "Not sure 1", labels2
    assert ph._FRAME_LABELS["by_tid"][3] == (None, "Not sure 2")
    # a single Not sure person is just "Not sure"
    ph._trk_status.pop(3)
    out3, labels3 = ph._v8_finalize([("person_1", 300), (None, None)], [1, 2], people[:2])
    assert labels3 == [None, "Not sure"], labels3


def test_doubled_skeleton_is_labelled_once():
    _fresh_hook()
    a = body(400)
    b = a.copy()
    b[:, :2] += rng.normal(0, 2.0, (17, 2))
    out, labels = ph._v8_finalize([("person_1", 300), ("person_1", 300)], [5, 6], [a, b])
    assert out[0][0] == "person_1" and out[1][0] is None and labels[1] == "__dup__", (out, labels)
    # two different people standing close are NOT merged
    out, labels = ph._v8_finalize([("person_1", 300), (None, None)], [5, 7], [body(400), body(470)])
    assert labels[1] != "__dup__"


def test_second_opinion_on_new_tracks():
    _fresh_hook()
    ph._named_cache[0] = 1e18
    ph._named_cache[1] = {"mom", "dad"}
    ph._signatures.setdefault("mom", ph._PersonSignature("mom"))
    ph._signatures.setdefault("dad", ph._PersonSignature("dad"))
    ph._V8.appearance_acc = 0.70
    # face and body both clearly say "mom" while the fingerprint alone could not decide
    ph._v8_raw = lambda t, p: ({"face": 0.62, "body": -0.7, "appearance": 0.76} if p == "mom"
                               else {"face": 0.05, "body": -3.6, "appearance": 0.62})
    m, why = ph._v8_decide_new(11, "?", "fingerprint unsure", set())
    assert m == "mom" and why.startswith("V8"), (m, why)
    # the fingerprint said dad, but face and body say mom: hold back the name
    m, why = ph._v8_decide_new(11, "dad", "fingerprint", set())
    assert m in ("?", "mom"), (m, why)
    # nothing strong either way: leave the fingerprint's answer alone
    ph._v8_raw = lambda t, p: {"appearance": 0.72}
    m, why = ph._v8_decide_new(11, "dad", "fingerprint", set())
    assert m == "dad", (m, why)


def test_face_cue_on_real_photos():
    """Uses LFW photos if they are on this machine (tests/calibrate_face.py fetches them)."""
    home = os.environ.get("LFW_HOME", "/tmp/claude-0/lfw")
    if not os.path.isdir(home):
        print("  (skipped: no LFW photos)")
        return
    from sklearn.datasets import fetch_lfw_people
    import cv2
    d = fetch_lfw_people(min_faces_per_person=30, resize=1.0, color=True, slice_=None, data_home=home,
                         download_if_missing=False)
    F = vi.FaceID()
    assert F.ok, F.why
    f = vi.Fusion(path=os.path.join(tempfile.mkdtemp(), "v8.json"))
    s = 40 / 105.0                                  # faces about 40 px wide, as across a room
    k = np.zeros((17, 3)); k[:, 2] = .9
    for i, (x, y) in {0: (125, 120), 1: (107, 110), 2: (143, 110), 3: (80, 115), 4: (170, 115),
                      5: (15, 235), 6: (235, 235)}.items():
        k[i, :2] = (x * s, y * s)

    def emb(i):
        im = (d.images[i] * (255 if d.images[i].max() <= 1.0 else 1)).astype("uint8")[:, :, ::-1]
        return F.embed(cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA), k)
    pa, pb = 0, 1
    ia, ib = np.where(d.target == pa)[0], np.where(d.target == pb)[0]
    f.learn_face("A", [e for e in (emb(i) for i in ia[:4]) if e])
    f.learn_face("B", [e for e in (emb(i) for i in ib[:4]) if e])
    hits = total = 0
    for idx, who in [(i, "A") for i in ia[4:14]] + [(i, "B") for i in ib[4:14]]:
        e = emb(idx)
        if e is None:
            continue
        ea = f.evidence({"face": f.face_sim([e], "A")})[0]
        eb = f.evidence({"face": f.face_sim([e], "B")})[0]
        total += 1
        hits += (ea > eb) == (who == "A")
    assert total >= 15 and hits / total >= 0.9, (hits, total)
    print("  face cue picked the right person in %d of %d new photos" % (hits, total))




def test_relink_after_a_short_loss():
    _fresh_hook()
    import time as _t
    ph._named_cache[0] = 1e18
    ph._named_cache[1] = {"mom", "dad"}
    ph._v8_raw = lambda t, p: {}
    now = _t.time()
    k = body(500)
    box = ph._kp_box(k)
    h = box[3] - box[1]
    # mom was last seen right there a second ago: the new track keeps her name
    ph._LAST_SEEN.clear()
    ph._LAST_SEEN["mom"] = (now - 1.0, (box[0] + box[2]) / 2 + 10, (box[1] + box[3]) / 2, h)
    ph._LAST_SEEN["dad"] = (now - 1.0, 1500.0, 400.0, h)
    pid, why = ph._v8_relink(9, 0, k, [box], set())
    assert pid == "mom", (pid, why)
    # both were there: ambiguous, decide the normal way
    ph._LAST_SEEN["dad"] = (now - 1.0, (box[0] + box[2]) / 2 - 12, (box[1] + box[3]) / 2, h)
    assert ph._v8_relink(9, 0, k, [box], set())[0] is None
    # gone for 10 s: too long to assume
    ph._LAST_SEEN.pop("dad")
    ph._LAST_SEEN["mom"] = (now - 10.0, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, h)
    assert ph._v8_relink(9, 0, k, [box], set())[0] is None
    # born right next to someone else: too risky
    ph._LAST_SEEN["mom"] = (now - 1.0, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, h)
    other = ph._kp_box(body(560))
    assert ph._v8_relink(9, 0, k, [box, other], set())[0] is None
    # the evidence says it is not her (face proven in this house): no re-link
    for _ in range(30):
        ph._V8.add_example("face", rng.normal(0.55, 0.1), True)
        ph._V8.add_example("face", rng.normal(0.08, 0.08), False)
    ph._v8_raw = lambda t, p: {"face": 0.02}
    assert ph._v8_relink(9, 0, k, [box], set())[0] is None
    ph._V8.stats["face"] = {"same": [], "diff": []}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
