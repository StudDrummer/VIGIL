"""ONE_NAME_V1 / LABEL_NUMBERS_V1: a name is never on two people at once, and
repeated unnamed labels are numbered the same way on the camera picture and
in the app."""
import os
import sys
import tempfile

import numpy as np

os.environ["VIGIL_REID_NOLOAD"] = "1"
os.environ["VIGIL_IDENTITY_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import person_hook as ph  # noqa: E402


def reset():
    ph._track_to_pid.clear(); ph._trk_status.clear(); ph._LBL_NUM.clear()
    ph._trk_fp.clear(); ph._trk_fp_t.clear()


def test_same_name_twice_keeps_the_stronger_claim():
    reset()
    ph._track_to_pid.update({5: "A"})                      # track 5 already holds A
    out = ph._one_name_each([("A", 0), ("A", 0), ("B", 0)], [7, 5, 9])
    assert out == [(None, 0), ("A", 0), ("B", 0)], out
    assert ph._trk_status[7] == "Not sure" and 7 not in ph._track_to_pid


def test_stale_map_entry_cannot_draw_a_second_name():
    reset()
    ph._track_to_pid.update({5: "A", 7: "A"})              # leftover from earlier frames
    out = ph._one_name_each([("A", 0), (None, 0)], [5, 7])
    assert out == [("A", 0), (None, 0)] and 7 not in ph._track_to_pid


def test_not_sure_numbering_is_stable_and_resets():
    reset()
    named = {"A"}
    ph._trk_status.update({1: "Not sure", 2: "Not sure", 3: "Not sure"})
    l1 = ph._labels([(None, 0), (None, 0), ("A", 0), (None, 0)], [1, 2, 4, 3], named)
    assert l1 == ["Not sure 1", "Not sure 2", None, "Not sure 3"], l1
    # order in the frame changes (sorted by size): numbers stay with the person
    l2 = ph._labels([(None, 0), (None, 0), (None, 0)], [3, 1, 2], named)
    assert l2 == ["Not sure 3", "Not sure 1", "Not sure 2"], l2
    # track 1 leaves: the others keep their numbers; a newcomer takes the free one
    ph._trk_status[8] = "Not sure"
    l3 = ph._labels([(None, 0), (None, 0), (None, 0)], [3, 2, 8], named)
    assert l3 == ["Not sure 3", "Not sure 2", "Not sure 1"], l3
    # only one left: plain "Not sure", and numbering starts over next time
    l4 = ph._labels([(None, 0)], [2], named)
    assert l4 == ["Not sure"]
    assert ph._labels([(None, 0), (None, 0)], [2, 3], named) == ["Not sure 1", "Not sure 2"]


def test_other_labels_numbered_separately():
    reset()
    ph._trk_status.update({1: "Visitor", 2: "Visitor", 3: "Not sure"})
    l = ph._labels([(None, 0), (None, 0), (None, 0), ("p7", 0), ("p8", 0)], [1, 2, 3, 4, 5], set())
    assert l == ["Visitor 1", "Visitor 2", "Not sure", "New person 1", "New person 2"], l


def test_frame_loop_never_shows_a_name_twice():
    reset()
    ph._signatures.clear()
    for p in "AB":
        ph._signatures[p] = ph._PersonSignature(p)
    ph._named_cache[0], ph._named_cache[1] = 1e18, {"A", "B"}
    ph._track_to_pid.update({1: "A", 2: "A"})               # the bug: both tracks think they are A
    kp = np.zeros((17, 3)); kp[:, 2] = 0.9
    kp[:, 0] = np.linspace(100, 160, 17); kp[:, 1] = np.linspace(100, 500, 17)
    kp2 = kp.copy(); kp2[:, 0] += 600
    ph.vigil_track_persons([kp, kp2], frame=np.zeros((720, 1280, 3), np.uint8), track_ids=[1, 2])
    shown = [v[0] for v in ph._DISPLAY.values() if v[0]]
    assert shown.count("A") == 1, ph._DISPLAY
    assert sum(1 for v in ph._DISPLAY.values() if v[0] is None and v[1] and v[1].startswith(("Not sure", "Identifying"))) == 1
