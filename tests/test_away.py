"""AWAY_V1: a person marked away (travelling) is never given to anyone in view, is not restored
after a restart, and is recognisable again as soon as they are marked back."""
import os
import sys
import tempfile

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


def test_away_dad_is_never_named():
    import person_hook as ph
    real = ph.os.path.join
    ph.os.path.join = lambda *a: Server.DB_PATH if a[-1] == "vigil.db" else real(*a)
    try:
        dad = C.post("/persons/new", json={"name": "Dad", "height_cm": 178}, headers=H).json["person_id"]
        me = C.post("/persons/new", json={"name": "Rushil", "height_cm": 175}, headers=H).json["person_id"]
        for p in (dad, me):
            ph._signatures[p] = ph._PersonSignature(p)
        ph._track_to_pid[7] = dad                        # a track currently labelled as Dad
        ph._away_cache[0] = 0; ph._named_cache[0] = 0
        assert dad in ph._named_ids()

        r = C.post("/persons/%s/away" % dad, json={"away": True}, headers=H)
        assert r.json == {"status": "ok", "away": True}
        p = next(x for x in C.get("/persons", headers=H).json if x["id"] == dad)
        assert p["away"] == 1 and p["away_since"], p

        ph._away_cache[0] = 0; ph._named_cache[0] = 0
        ph._drop_away()
        assert dad not in ph._signatures                  # cannot be matched to anyone
        assert 7 not in ph._track_to_pid                  # the wrong label is taken off
        assert dad not in ph._named_ids() and me in ph._named_ids()

        C.post("/persons/%s/away" % dad, json={"away": False}, headers=H)
        ph._away_cache[0] = 0; ph._named_cache[0] = 0
        reloaded = []
        ph.reload_known_persons = lambda: reloaded.append(1)
        ph._drop_away()
        assert reloaded == [1]                            # back home: restored straight away
        assert dad in ph._named_ids() and ph._away_ids() == set()
        assert C.post("/persons/nobody/away", json={"away": True}, headers=H).status_code == 404
    finally:
        ph.os.path.join = real


if __name__ == "__main__":
    test_away_dad_is_never_named()
    print("away: OK")
