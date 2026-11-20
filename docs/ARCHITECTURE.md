# VIGIL System Architecture

VIGIL runs the same pipeline in one of two places: on a Jetson in the room,
or on the VIGIL Cloud server with a small relay box next to the camera.

## Hardware

- Camera: a Tapo IP camera with RTSP (C210 or C225), fixed in place
- In-room processing: NVIDIA Jetson Orin Nano (8 GB)
- Cloud homes: a Raspberry Pi relay box next to the router, and a Google Cloud
  VM (about 2 CPU cores per camera)

## Components

| Part | Where it lives | What it does |
|---|---|---|
| `jetson-backend/activity_monitor.py` | Jetson or site container | Reads the camera, runs pose, falls, gait, posture and activity |
| `jetson-backend/person_hook.py`, `vigil_identity.py`, `vigil_reid.py` | same | Tells people apart: tracking, appearance, body proportions, walking pattern, face |
| `jetson-backend/Server.py` | same | REST API and SQLite database for one camera; sends alerts |
| `jetson-backend/vigil_face_service.py` | same, own virtualenv | Facial cues |
| `jetson-backend/Night_anamaly.py`, `person_baseline.py`, `person_patterns.py` | same | Nightly baselines, routine and anomaly jobs |
| `relay/vigil_relay.py`, `relay/wifi_setup.py` | relay box | Finds the camera, forwards its video encrypted, handles Wi-Fi setup |
| `cloud/gateway/gateway.py` | cloud | Accounts, which cameras each person may see, push and email alerts, proxy to each camera's API |
| `cloud/vigilctl.py` | cloud | Adds, removes and tunes cameras; writes the Docker and MediaMTX configuration |
| `ios-app/App.tsx` | iPhone | The app: Today, Trends, Alerts, AI Insights, Settings, setup |

## Data flow

1. The camera streams RTSP H.264. In a cloud home the relay box forwards it
   over SRT (encrypted) to MediaMTX on the server.
2. YOLOv8n-pose gives 17 keypoints per person; ByteTrack follows each person
   between frames.
3. Per person: gait from heel strikes, posture from shoulders and hips,
   activity state, and identity from several independent cues.
4. Falls: torso angle, a standing-legs check and furniture zones (so lying on
   a bed or sofa is not a fall), then a floor-time confirmation.
5. Measures are written to SQLite through the Flask API; daily summaries and
   per-person baselines are rebuilt from them.
6. On a fall or a long stillness the API notifies the gateway, which sends an
   Apple push notification (time-sensitive, so it reaches phones in a Focus)
   and an email to everyone with access to that camera. One fall gives one
   alert; at most three fall alerts per camera in half an hour; an alert no
   phone received is retried for 10 minutes; an unanswered fall is repeated as
   urgent after 5 minutes. ntfy is only a fallback when no phone got the push.
   During the fall lab, lab mode records falls quietly and leaves them out of
   the accuracy numbers.
7. The camera checks every 30 s that it has not been moved (comparing fixed
   features with views of the room in different light) and asks for the bed
   and couch to be redrawn only after a clear shift has lasted 10 minutes.
8. The app reads the API (directly on the home network, or through the
   gateway with an account) and shows live view, metrics, trends and insights.

## Deployment and updates

- `main` is what the developer's home runs; `stable` is what deployed homes run.
- Relay boxes check `stable` every 15 minutes and the cloud server every 10,
  and update themselves. `tools/ship.sh` promotes `main` to `stable`, shows
  status and can undo the last promotion.
- A home Jetson can also be registered as a cloud camera over a private
  Tailscale link, so one account sees both kinds of home.

## Privacy

- Video is analysed in memory as it arrives and is never recorded.
- Only computed measures are stored, plus identity templates (numbers, not
  images), which stay with that camera and are deleted with the person.
- The live view shows stick figures by default; video is opt-in and encrypted.
  An owner can lock a camera to stick figures only (enforced by the gateway
  and the camera), and every opening of live video is logged.
- A person marked away is never given to anyone the camera sees.
- Each camera has its own database and its own container; the gateway checks
  the signed-in person's access on every request.
- Biometric templates, databases and credentials are excluded from this
  repository by `.gitignore`.
