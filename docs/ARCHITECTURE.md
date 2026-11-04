# VIGIL System Architecture

## Hardware
- Camera: Tapo C225 (RTSP H.264, 1080p)
- Processing unit: NVIDIA Jetson Orin Nano (8GB LPDDR5, 1024 CUDA cores)
- Network: Tailscale VPN (WireGuard) — private, encrypted, not internet-accessible

## Software Stack
- OS: Ubuntu 22.04 LTS (Jetson)
- Python 3.10: Flask, OpenCV, Ultralytics YOLOv8, NumPy, SciPy, Twilio
- iOS: React Native 0.74, TypeScript, react-native-health (HealthKit)
- Database: SQLite (local to Jetson)

## Data Flow
1. Camera streams RTSP H.264 → OpenCV VideoCapture (buffer=1, 8-frame flush)
2. YOLOv8n-pose runs inference → 17 keypoints per detected person
3. GaitTracker processes keypoints → gait speed, cadence, posture score
4. Fall detection FSM evaluates hip velocity → alert if FALLEN state confirmed
5. Metrics POST to Flask API → SQLite write → daily_summary rebuild
6. iOS app polls API every 4s → renders metrics on Today screen
7. On fall/stillness → send_sms() → Twilio API → SMS to emergency contact

## Privacy Architecture
- No raw video stored or transmitted
- All processing on local Jetson hardware
- Only computed metrics leave the edge device
- Tailscale VPN: all traffic encrypted, no public internet exposure
- No cloud storage of any health data

## Telling people apart (IDENTITY_V8, `vigil_identity.py`)
Five signals each give a vote for every known person; the votes are added
and a name is given only when one person is clearly ahead.

| Signal | What it measures | Notes |
| --- | --- | --- |
| Face | 128-number signature (OpenCV YuNet + SFace) | Only when the face is turned to the camera and big enough. Strongest signal. |
| Appearance | Body-crop fingerprint (DINOv2, `vigil_reid.py`) | Mostly clothing. Can strongly say "same", only mildly "different". |
| Proportions | Shoulder, hip, arm, thigh, shin relative to torso | From upright poses only. |
| Height | True height, distance from the camera removed | |
| Gait | Steps per second, stride, arm swing, bounce | Only while walking. |

Every confirm and correct in the app is a verified example: each signal's
score for the right person and for everyone else is recorded, and a small
calibration model per signal is refit. A signal that tells people apart in
this home earns a bigger vote; one that does not is turned down or off.
`GET /identity/report` shows each signal's learned weight and accuracy.

Face recognition is per camera: `POST /identity/face {"on": false}` (or the
switch in the app's Settings) turns it off and deletes every stored face
signature. The identity model keeps no face images, only the numbers (the
small per-person photo shown in the app is separate and existed before).
