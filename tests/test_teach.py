"""TEACH_V1 / HEIGHT_PER_PERSON_V1: naming someone new from the person in
view creates them and teaches identity; each person can have their own height."""
import os
import sys
import tempfile

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.makedirs(os.path.join(home, "passive-health-moniter"))
os.environ.setdefault("VIGIL_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import Server  # noqa: E402

Server.init_db()
C = Server.app.test_client()
H = {"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]}


def test_new_person_from_track_and_height():
    r = C.post("/tracks/12/new", json={"name": "Nani", "height_cm": 152, "is_resident": True}, headers=H)
    assert r.status_code == 200, r.json
    pid = r.json["person_id"]
    people = {p["id"]: p for p in C.get("/persons", headers=H).json}
    assert people[pid]["name"] == "Nani" and people[pid]["height_cm"] == 152 and people[pid]["is_resident"] == 1
    c = Server.get_db()
    row = c.execute("SELECT src, dst FROM person_corrections ORDER BY id DESC").fetchone()
    assert (row["src"], row["dst"]) == ("track:12", pid)          # the camera learns from it
    c.execute("UPDATE config SET resident_person_id=?", (pid,))
    c.commit(); c.close()
    assert C.post("/persons/%s/height" % pid, json={"height_cm": 155}, headers=H).json["height_cm"] == 155
    assert Server.get_config()["resident_height_cm"] == 155          # the resident's profile follows
    assert C.post("/persons/%s/height" % pid, json={"height_cm": 400}, headers=H).status_code == 400
    assert C.post("/tracks/3/new", json={"name": " "}, headers=H).status_code == 400


if __name__ == "__main__":
    test_new_person_from_track_and_height()
    print("ok")
