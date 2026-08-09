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
