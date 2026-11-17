"""ONE_ALERT_V1: at most three fall alerts from a camera in half an hour, and an alert that
reached the app is not sent a second time through ntfy."""
import os
import sys
import tempfile
import types

home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.makedirs(os.path.join(home, "passive-health-moniter"))
os.environ.setdefault("VIGIL_API_KEY", "test-key")
os.environ["VIGIL_NTFY_TOPIC"] = "t"
os.environ["VIGIL_PUSH_URL"] = "http://gateway/internal/push/x"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jetson-backend"))
import Server  # noqa: E402

Server.init_db()
Server.send_sms = lambda *a, **k: None
C = Server.app.test_client()
H = {"X-VIGIL-Key": os.environ["VIGIL_API_KEY"]}


def test_burst_cap():
    import time
    real = time.time
    clock = [real()]
    time.time = lambda: clock[0]
    try:
        codes = []
        for i in range(40):                      # one a minute for 40 minutes
            clock[0] += 60
            codes.append(C.post("/alert/fall", json={"message": "x"}, headers=H).status_code)
        assert codes[:3] == [200, 200, 200] and set(codes[3:29]) == {429}, codes
        assert codes[30:33] == [200, 200, 200], codes       # half an hour later it may speak again
        assert codes.count(200) <= 6, codes
    finally:
        time.time = real


def test_ntfy_is_only_the_fallback():
    calls = []

    class R:
        ok = True
        def __init__(self, phones): self._p = phones
        def json(self): return {"phones": self._p}

    def post(url, **k):
        calls.append(url)
        return R(PHONES[0])
    sys.modules["requests"] = types.SimpleNamespace(post=post)
    PHONES = [1]
    Server.send_push("VIGIL", "possible fall")
    assert calls == ["http://gateway/internal/push/x"], calls          # the app got it: no ntfy copy
    calls.clear(); PHONES[0] = 0
    Server.send_push("VIGIL", "possible fall")
    assert len(calls) == 2 and "ntfy" in calls[1], calls                # nobody's phone got it: ntfy steps in


if __name__ == "__main__":
    test_burst_cap(); test_ntfy_is_only_the_fallback(); print("ok")
