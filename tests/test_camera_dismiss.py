"""CAM_MOVED_V3: "it did not move" adds the current view to the remembered views (the same room
in other light) instead of replacing the reference; redrawing the areas starts over."""
import os
import shutil
import sys
import tempfile

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.makedirs(os.path.join(home, "passive-health-moniter"))
os.environ.setdefault("VIGIL_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import Server  # noqa: E402

d = tempfile.mkdtemp()
Server._MOVED_FLAG = os.path.join(d, "camera_moved.json")
Server._SCENE_REF = os.path.join(d, "scene_reference.jpg")
Server._SCENE_BANK = os.path.join(d, "scene_refs")
RAW = "/dev/shm/vigil_raw.jpg"


def test_dismiss_adds_view_and_redraw_resets():
    had = os.path.exists(RAW)
    if not had:
        open(RAW, "wb").write(b"jpegbytes")
    try:
        open(Server._SCENE_REF, "wb").write(b"morning")
        open(Server._MOVED_FLAG, "w").write("{}")
        C = Server.app.test_client()
        r = C.post("/camera/moved/dismiss", headers={"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]})
        assert r.status_code == 200
        assert open(Server._SCENE_REF, "rb").read() == b"morning"          # the reference is kept
        assert len(os.listdir(Server._SCENE_BANK)) == 1                    # this light is remembered
        assert not os.path.exists(Server._MOVED_FLAG)
        Server._camera_view_accepted()                                     # areas redrawn: start over
        assert os.listdir(Server._SCENE_BANK) == []
    finally:
        if not had:
            os.remove(RAW)


if __name__ == "__main__":
    test_dismiss_adds_view_and_redraw_resets()
    print("camera dismiss: OK")
