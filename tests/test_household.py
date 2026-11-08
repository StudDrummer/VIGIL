"""HOUSEHOLD_V1: people are added during setup (before the camera has seen them),
with consent recorded; PHOTO_SEED_V1: a clear photo seeds face matching once."""
import base64
import os
import sys
import tempfile
import types

import numpy as np

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.makedirs(os.path.join(home, "passive-health-moniter"))
os.environ.setdefault("VIGIL_API_KEY", "test-key")
os.environ["VIGIL_REID_NOLOAD"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import Server  # noqa: E402

Server.init_db()
C = Server.app.test_client()
H = {"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]}


def test_add_people_and_consent():
    r = C.post("/persons/new", json={"name": "Shivani", "is_resident": True, "height_cm": 160}, headers=H)
    assert r.status_code == 200, r.json
    pid = r.json["person_id"]
    assert C.post("/persons/new", json={"name": " shivani "}, headers=H).status_code == 409     # no duplicates
    assert C.post("/persons/new", json={"name": "X", "height_cm": 20}, headers=H).status_code == 400
    assert C.post("/persons/new", json={"name": ""}, headers=H).status_code == 400
    assert C.post("/persons/%s/consent" % pid, json={"by": "Rushil"}, headers=H).json["status"] == "ok"
    p = next(x for x in C.get("/persons", headers=H).json if x["id"] == pid)
    assert p["name"] == "Shivani" and p["is_resident"] == 1 and p["height_cm"] == 160 and p["consent_at"], p
    assert C.post("/persons/nobody/consent", json={}, headers=H).status_code == 404
    C.post("/persons/%s/consent" % pid, json={"given": False}, headers=H)
    assert next(x for x in C.get("/persons", headers=H).json if x["id"] == pid)["consent_at"] is None


def test_photo_seeds_face_once():
    import person_hook as ph
    ph._SEED_FILE = os.path.join(home, "seeds.json")
    pid = C.post("/persons/new", json={"name": "Myra"}, headers=H).json["person_id"]
    import cv2
    ok, jpg = cv2.imencode(".jpg", np.full((200, 200, 3), 128, np.uint8))
    C.post("/persons/%s/photo/set" % pid, json={"photo_b64": base64.b64encode(jpg.tobytes()).decode()}, headers=H)
    learned = []
    ph._V8 = types.SimpleNamespace(learn_face=lambda p, e: learned.append(p) or 1, save=lambda force=False: None)
    ph._V8_FACE = types.SimpleNamespace(ok=True, mode="yunet", embed_photo=lambda img: (np.ones(128) / np.sqrt(128), 0.9))
    real = ph.os.path.join
    ph.os.path.join = lambda *a: Server.DB_PATH if a[-1] == "vigil.db" else real(*a)
    try:
        ph._SEED["t"] = 0
        ph._seed_faces_from_photos()
        assert learned == [pid], learned
        ph._SEED["t"] = 0
        ph._seed_faces_from_photos()
        assert learned == [pid]                    # the same photo is not learned twice
    finally:
        ph.os.path.join = real


if __name__ == "__main__":
    test_add_people_and_consent(); test_photo_seeds_face_once(); print("ok")
