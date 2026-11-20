"""REMOTE_SITE_V1: a camera whose server runs elsewhere (the home Jetson) is listed
for the gateway with its own address and key, gets no container and no video path."""
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cloud"))
import vigilctl as v  # noqa: E402

d = tempfile.mkdtemp()
v.HERE = d; v.CONF = os.path.join(d, "config"); v.STATE = os.path.join(v.CONF, "state.json")
v.compose_up = lambda st, quiet=False: None
os.makedirs(v.CONF)
st = {"domain": "api.example.com", "reader_pass": "r", "cameras": []}
v.save(st)
args = types.SimpleNamespace(id="home02", name="India home 2", home="", room="", tz="Asia/Kolkata", pull_url=None, hospital=False, floor_alert_s=60, no_face=False, domain=None)
try:
    v.cmd_add_camera(args)
except SystemExit:
    pass
v.cmd_add_remote(types.SimpleNamespace(id="home00", name="Home (test)", url="http://100.64.0.10:5001/",
                                       key="jetsonkey", home="Bellevue", tz="America/Los_Angeles"))
sites = {s["id"]: s for s in json.load(open(os.path.join(v.CONF, "sites.json")))["sites"]}
assert sites["home00"]["internal_url"] == "http://100.64.0.10:5001" and sites["home00"]["api_key"] == "jetsonkey", sites["home00"]
assert sites["home00"].get("remote") is True
compose = open(os.path.join(d, "docker-compose.yml")).read()
mtx = open(os.path.join(v.CONF, "mediamtx.yml")).read()
assert "site-home00" not in compose and "home00:" not in mtx.split("paths:")[1], "remote camera must get no container or video path"
if "home02" in sites:
    assert "site-home02" in compose and sites["home02"]["internal_url"] == "http://site-home02:5001"
print("ALL PASS")
