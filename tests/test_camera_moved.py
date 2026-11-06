"""CAM_MOVED_V1: a shifted or different view is flagged after 4 checks; the
same view under brighter or dimmer light is not."""
import json
import os
import shutil
import sys
import tempfile
import types

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "jetson-backend"))
sys.modules.setdefault("ultralytics", types.SimpleNamespace(YOLO=None))
import activity_monitor as am  # noqa: E402

T = [1000.0]
am.time.time = lambda: T[0]




def load_scene():
    for p in sys.path:
        q = os.path.join(p, "ultralytics", "assets", "bus.jpg")
        if os.path.exists(q):
            img = cv2.imread(q)
            return cv2.resize(img, (1280, 720)) if img.shape[1] < 1280 else img[:720, :1280]
    rng = np.random.default_rng(0)
    img = np.full((720, 1280, 3), 120, np.uint8)
    for _ in range(300):
        x, y = rng.integers(0, 1200), rng.integers(0, 680)
        cv2.rectangle(img, (int(x), int(y)), (int(x) + 60, int(y) + 30), tuple(int(c) for c in rng.integers(0, 255, 3)), -1)
    return img


def watch_with_ref(ref):
    d = tempfile.mkdtemp()
    w = am.CameraMoveWatch()
    w.base = d
    w.ref_path = os.path.join(d, "scene_reference.jpg")
    w.flag_path = os.path.join(d, "camera_moved.json")
    w.zones_path = os.path.join(d, "fall_zones.json")
    json.dump([{"x1": 0.1, "y1": 0.1, "x2": 0.3, "y2": 0.3}], open(w.zones_path, "w"))
    cv2.imwrite(w.ref_path, ref)
    return w


def checks(w, frame, n=5):
    for _ in range(n):
        T[0] += 31
        w.update(frame, False)
    return os.path.exists(w.flag_path)


BASE = load_scene()


def test_same_view_not_flagged():
    assert not checks(watch_with_ref(BASE), BASE.copy())


def test_lighting_change_not_flagged():
    dim = cv2.convertScaleAbs(BASE, alpha=0.6, beta=10)
    bright = cv2.convertScaleAbs(BASE, alpha=1.2, beta=15)
    assert not checks(watch_with_ref(BASE), dim)
    assert not checks(watch_with_ref(BASE), bright)


def test_shifted_view_flagged():
    M = np.float32([[1, 0, 160], [0, 1, 40]])
    moved = cv2.warpAffine(BASE, M, (BASE.shape[1], BASE.shape[0]), borderMode=cv2.BORDER_REFLECT)
    assert checks(watch_with_ref(BASE), moved)


def test_rotated_view_flagged():
    M = cv2.getRotationMatrix2D((640, 360), 12, 1.0)
    moved = cv2.warpAffine(BASE, M, (BASE.shape[1], BASE.shape[0]), borderMode=cv2.BORDER_REFLECT)
    assert checks(watch_with_ref(BASE), moved)


def test_different_room_flagged():
    other = cv2.flip(BASE, -1)
    assert checks(watch_with_ref(BASE), other, n=11)


def test_evening_lamps_not_compared():
    # CAM_MOVED_V2: much darker or brighter than the saved view is skipped, not judged
    evening = cv2.flip(cv2.convertScaleAbs(BASE, alpha=0.5, beta=0), -1)
    assert not checks(watch_with_ref(BASE), evening, n=12)


def test_people_in_view_not_compared():
    w = watch_with_ref(BASE)
    other = cv2.flip(BASE, -1)
    for _ in range(12):
        T[0] += 31
        w.update(other, True)
    assert not os.path.exists(w.flag_path)


def test_infrared_night_not_compared():
    ir = cv2.cvtColor(cv2.cvtColor(BASE, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    assert not checks(watch_with_ref(BASE), cv2.flip(ir, -1))


def test_needs_four_checks():
    M = np.float32([[1, 0, 160], [0, 1, 40]])
    moved = cv2.warpAffine(BASE, M, (BASE.shape[1], BASE.shape[0]), borderMode=cv2.BORDER_REFLECT)
    w = watch_with_ref(BASE)
    assert not checks(w, moved, n=3)




def test_person_in_front_not_flagged():
    blocked = BASE.copy()
    cv2.rectangle(blocked, (450, 80), (800, 720), (60, 50, 40), -1)   # a person a third of the frame wide
    assert not checks(watch_with_ref(BASE), blocked)


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
