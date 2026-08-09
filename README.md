# VIGIL — Passive Health Monitoring for Assisted Living

**Rushil Mohan | The Overlake School, Bellevue WA | 2026**

[![Website](https://img.shields.io/badge/Website-vigilhealth.me-32D74B)](https://vigilhealth.me)
[![Contact](https://img.shields.io/badge/Email-hello@vigilhealth.me-0A84FF)](mailto:hello@vigilhealth.me)

## The Problem

My grandmother's blood sugar went dangerously high for months before anyone noticed. Her blurred vision looked like normal aging until a routine checkup caught it. Gradual health decline is easy to miss — especially in assisted living environments where staff ratios are stretched and residents often don't report their own symptoms.

## What VIGIL Does

VIGIL is a passive, camera-based health monitoring system for assisted living facilities. It continuously tracks each resident's walking speed, posture, and daily activity patterns using computer vision — no wearable required — and immediately texts a caregiver when it detects a fall or unusual stillness.

Over 7 days, VIGIL builds a personal baseline for each resident and uses anomaly detection (z-score > 2σ) to flag meaningful deviations from their own normal — not population averages, but their specific individual pattern. The system also integrates Apple Watch data (heart rate, HRV, SpO2, sleep, steps) for residents who own one.

## Architecture
[IP Camera] ──RTSP──► [NVIDIA Jetson Orin Nano]
│
YOLOv8n-pose (30 FPS)
17-keypoint skeleton extraction
GaitTracker: heel-strike cadence,
normalized gait speed, posture scoring
Fall detection FSM (2.5s confirmation)
Per-person biometric re-identification
7-day rolling baseline + z-score alerts
│
Flask REST API (Tailscale VPN)
│
┌───────────────┴───────────────┐
iOS App Twilio SMS
(caregiver dashboard) (instant fall/stillness
live camera, metrics, alerts to designated
trends, alerts emergency contact)
## Key Technical Components

### Computer Vision Pipeline (`jetson-backend/activity_monitor.py`)
- **Model:** YOLOv8n-pose, real-time 17-keypoint human pose estimation
- **GaitTracker class:** Computes cadence via heel-strike timing (deque-bounded lists prevent memory leak), gait speed via keypoint displacement normalized to body height, posture score from trunk lean + shoulder asymmetry + head forward angle
- **Fall Detection FSM:** IDLE → DESCENDING → FALLEN state machine, confirmed over 2.5s window. Hip velocity sign corrected for Vision coordinate system (Y increases downward). Events fire exactly once per fall, not once per frame.
- **RTSP reliability:** Single-frame buffer (CAP_PROP_BUFFERSIZE=1), 8-frame flush before retrieve, automatic reconnect with buffer re-application

### Person Intelligence (`jetson-backend/person_hook.py`, `person_baseline.py`)
- **Persistent re-identification:** Height-based biometric signature matching across service restarts — persons are recognized without facial recognition or invasive biometrics
- **Baseline modeling:** 7-day rolling mean + standard deviation per metric per named individual, rebuilt on every data write
- **Anomaly detection:** Z-score alerts fire when readings exceed 2σ from personal baseline, with configurable cooldown to prevent alert fatigue
- **Pattern analysis:** Time-of-day activity heatmaps, absence detection when a resident isn't seen during their expected active hours

### iOS Caregiver Dashboard (`ios-app/App.tsx`)
- React Native 0.74, TypeScript, 1,900+ lines
- Live camera snapshot polling (2.5s), real-time metric refresh (4s)
- HealthKit integration: 9 data types, SpO2 ratio-to-percentage normalization
- PersonDetailScreen: 14-day trend charts, week-over-week comparison
- Identification modal with auto-captured photo, named resident tracking
- AppState foreground refresh, demo mode for offline demonstration

### Backend (`jetson-backend/Server.py`)
- Flask REST API, 30+ routes, SQLite with daily summary aggregation
- HMAC timing-safe API key authentication
- Rate-limited SMS alerts (300s cooldown) via Twilio A2P 10DLC
- Credentials via environment variables only (EnvironmentFile systemd pattern)

## Tech Stack

| Layer | Technology |
|---|---|
| Computer Vision | YOLOv8n-pose, OpenCV |
| Edge Hardware | NVIDIA Jetson Orin Nano (8GB) |
| Backend | Python 3.10, Flask, SQLite |
| iOS App | React Native 0.74, TypeScript |
| Health Data | Apple HealthKit |
| Transport | Tailscale VPN (WireGuard) |
| Alerts | Twilio A2P 10DLC SMS |

## Research Questions (for ISEF / STS)

1. Can passive gait analysis via consumer-grade cameras establish statistically significant personal baselines for elderly individuals in assisted living settings?
2. Do deviations from individual gait baselines (z-score > 2σ) predict clinically reported health events (falls, illness, hospitalization) earlier than conventional observation?
3. What is the minimum observation window required to establish a reliable individual gait baseline?

## Current Status

- System deployed and tested in home environment (Bellevue, WA)
- Pilot deployment discussions ongoing with local assisted living facilities
- iOS app in TestFlight, pending App Store submission
- Twilio A2P 10DLC registration approved, SMS alerts functional

## Website

[vigilhealth.me](https://vigilhealth.me)

## Contact

Rushil Mohan — hello@vigilhealth.me
