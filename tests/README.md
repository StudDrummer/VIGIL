# Tests

| Kind | Files | Needs |
|---|---|---|
| Unit and logic tests | every `test_*.py` and `test_*.sh` except the four below | nothing: `bash tests/run_all.sh` |
| Live validation | `test_end_to_end.py`, `test_falls.py`, `test_multiperson.py`, `test_reid_safe.py` | the camera service running on the Jetson |
| Labs | `fall_lab.py`, `identity_lab.py` | the Jetson, the camera and people; run with `--check` first |
| Calibration | `calibrate_gait.py`, `calibrate_face.py`, `face_calibrate_v2.py` | the Jetson and the camera |

The labs save their results after every trial and resume if the connection
drops. Each packs a results file in the home folder
(`vigil_fall_lab.tar.gz`, `vigil_identity_lab.tar.gz`). The fall lab's
recordings can be replayed against the current code with `tools/fall_eval.py`.
