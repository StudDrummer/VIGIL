# VIGIL — Passive Health Monitoring for Assisted Living

**Rushil Mohan · The Overlake School, Bellevue WA**

[vigilhealth.me](https://vigilhealth.me) · hello@vigilhealth.me

A camera-based system that watches for falls and tracks each resident's
walking, posture and daily routine against their own personal baseline —
no wearable, no cloud, nothing stored off the device.

---

## Why

My grandmother's blood sugar ran dangerously high for months before anyone
noticed. Her blurred vision looked like ordinary ageing until a routine
checkup caught it.

Assisted living has the same problem at scale: roughly 96% of facilities
report staffing shortages, and a resident may be checked on every few
hours. Gradual change happens in between. VIGIL watches during that gap.

---

## What it measures, and how accurate it is

Every number below was measured against ground truth, not estimated.
Where VIGIL is wrong, the error is stated.

| Measure | Validation method | Result |
|---|---|---|
| Cadence | 60 s walk, steps counted aloud | **−0.4%** (89.6 vs 90 spm) |
| Gait speed | 20 m walked, stopwatch timed | **+10.6%** after per-resident calibration |
| Posture | presence in readings while in view | **6 of 6** |
| Fall detection | 8 controlled scenarios on a mattress | **3/3 detected, 0/5 false alarms** |
| Identity | walk, leave, return at new distance | **1 identity**, held across restarts |
| Facial landmarks | 4 conditions incl. talking control | **90%** detection rate |

**Known limits.** Gait speed reads about 10% high; the bias cancels in
baseline comparisons but the absolute value should not be quoted
clinically. Readings vary between 20-second windows, so VIGIL is reliable
as a trend over days, not as a single measurement. Routine signals
require the camera to be in the resident's own living space.

---

## How it works

```
IP camera ──RTSP──► Jetson Orin Nano (~11 fps)
                        │
           YOLOv8n-pose, 17 keypoints
           Gait: heel-strike peak detection, wall-clock intervals
           Falls: skeleton-box geometry + descent speed + zones
           Identity: ByteTrack + torso colour + body proportion
                        │
           Flask REST API ── SQLite (on device)
                        │
        ┌───────────────┴────────────────┐
   iOS app                          Twilio SMS
   live view, metrics,              fall and stillness
   trends, insights                 alerts to a caregiver
```

A separate process handles facial cues in its own virtualenv, so its
dependencies cannot disturb the main pipeline.

---

## Engineering notes

Several of these were found only by measuring something that looked fine.

**Fall detection.** The original test compared ankle height against a
threshold derived from the ankle-hip distance itself, which reduces
algebraically to `ankle_y > hip_y` — always true. It never detected a
single heel strike. Rebuilt around the bounding box of all confident
keypoints: aspect ratio and height collapse identify a floor posture, and
a required rapid descent distinguishes falling from lying down. Furniture
zones mark beds and sofas, because no speed threshold separates "fell" from
"went to bed".

**Identity.** Height in pixels varied 34% for one person within a single
appearance — more than the real difference between two siblings. Matching
loosely enough to hold one person merged two; tightly enough to separate
two split one into six. Both were measured. Adding a torso colour
histogram, compared by correlation, separated them: 0.89 same-person
versus −0.08 different-person. Colour fails across clothing changes where
height is stable, so the two signals cover each other.

**Frame rate.** Profiling showed inference at 36 ms and 211 ms spent
flushing stale RTSP frames. A threaded reader that keeps only the newest
frame took the pipeline from 3.5 to 11.5 fps, which is what made
frame-to-frame tracking viable at all.

**Facial cues.** Detection sat at 0% on full-resolution frames: MediaPipe
downscales its input, so a 60-pixel face becomes about 6 pixels. Cropping
the head from the pose keypoints and upscaling brought it to 90%.

---

## AI Insights

Insights compare each resident against their own baseline and cite what a
pattern is associated with. They never name a condition.

- **Gait speed** vs a 14-day personal baseline, with the published 0.8
  and 1.0 m/s fall-risk cut points shown as context — along with the fact
  that gait speed alone has low discriminative accuracy
- **Health signals** — resting heart rate, HRV, SpO2, respiratory rate,
  sleep and activity, flagged when several deviate together
- **Daily routine** — morning start time and night-time activity
- **Activity pattern** — hourly profile plus unusual days identified by an
  isolation forest trained only on that resident's own history

An earlier version scored 17 conditions using models trained on
synthetically generated data. Every probability came out at the 0.05
floor. It was removed rather than displayed: a health claim whose
methodology cannot be validated should not ship.

---

## Privacy

Video is processed on the device and never stored or transmitted. Only
computed metrics are kept, in a local database. Identity uses body
proportions and clothing colour, never facial recognition — no biometric
template of any resident is created or stored. The camera is fixed and
does not follow anyone.

---

## Repository

```
ios-app/          React Native app (TypeScript)
jetson-backend/   camera pipeline, REST API, nightly analysis jobs
tests/            validation harnesses used to produce the table above
docs/             architecture notes
```

The test harnesses are included deliberately. `test_falls.py`,
`test_multiperson.py` and `test_end_to_end.py` are how the numbers above
were produced and can be re-run to check them.

---

## Status

Deployed and running continuously. Pilot discussions underway with
assisted living facilities in Bellevue, WA. iOS app prepared for App Store
submission with a self-contained demo mode.

Not a medical device. VIGIL reports measured changes; it does not
diagnose.
