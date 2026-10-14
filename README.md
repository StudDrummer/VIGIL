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

Assisted living has the same problem at scale: in a 2022 AHCA/NCAL survey,
63% of assisted living providers reported staffing shortages and 98% had
asked staff to work overtime. A resident may be checked on every few
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
| Fall detection (v4.2) | staged falls and everyday movements on a mattress | **2/3 falls detected** (chair slump missed), **0/5 false alarms** |
| Fall detection (v4.3) | replayed 12 real alerts from home use | 3 everyday false alarms (bending over, leaning close, sitting on the couch) no longer fire; staged falls still do |
| Identity (v6) | two home sessions, family of four | 9 phantom people in one session, **0** in the next, after adding "Not sure" |
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
           Falls: torso angle + standing-legs check + furniture zones
           Identity: ByteTrack + DINOv2 appearance fingerprints + memory
                        │
           Flask REST API ── SQLite (on device)
                        │
        ┌───────────────┴────────────────┐
   iOS app                          push notifications
   live view, metrics,              (SMS via Twilio once
   trends, insights                 carrier approval completes)
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

**Identity, first versions.** Height in pixels varied 34% for one person within a single
appearance — more than the real difference between two siblings. Matching
loosely enough to hold one person merged two; tightly enough to separate
two split one into six. Both were measured. Adding a torso colour
histogram, compared by correlation, separated them: 0.89 same-person
versus −0.08 different-person. Colour fails across clothing changes where
height is stable, so the two signals cover each other.

**Identity, v6.** In daily family use that still failed: VIGIL labelled
the developer as his mother, and once asked "who is this?" 23 times in a
day. A wrong name corrupts two people's baselines, so it is not cosmetic.
Face recognition was rejected (biometric templates of vulnerable adults).
Instead, Meta's DINOv2 turns each body crop into a 384-number appearance
fingerprint; each person keeps a memory of up to 80, and every caregiver
confirmation or correction is pinned permanently. The pass mark is learned
from how alike different named people are. When two people fit, VIGIL
shows "Not sure" instead of guessing, and only strong matches or taps
teach it a new look, so one wrong guess cannot snowball. See
`jetson-backend/vigil_reid.py`.

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

Video is processed on the device and never recorded or sent to the cloud.
The app's live view shows a stick figure by default; a caregiver can open
the camera view over a private encrypted network (Tailscale). Only
computed metrics are kept, in a local database. Identity uses appearance
fingerprints (numbers, no images), never facial recognition. The camera is fixed and
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

Running continuously in the developer's home since late September 2026,
tested on family members only. Free pilots are offered to assisted living
communities near Bellevue, WA. The iOS app includes a self-contained,
clearly labelled demo mode.

---

## Open-source software and models used

| Component | Used for |
|---|---|
| [Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics) (pose) + ByteTrack | 17 body keypoints per person; frame-to-frame tracking |
| [Meta DINOv2](https://github.com/facebookresearch/dinov2) ViT-S/14 via PyTorch | appearance fingerprints for telling people apart |
| [Google MediaPipe](https://github.com/google-ai-edge/mediapipe) | eye and jaw openness (fatigue cues) |
| OpenCV, NumPy, SciPy, scikit-learn | video capture, geometry, statistics, isolation forest |
| Flask + SQLite | on-device REST API and database |
| React Native, TypeScript, react-native-svg, react-native-health | iOS app and Apple HealthKit |

## Use of AI

Claude (Anthropic), including Claude Code, was the main coding assistant
and wrote a large share of this code; it also helped find published
research. Website drafts came from Claude. The developer chose the problem
and architecture, deployed the hardware, ran every validation listed
above, and decided what to keep, change or delete.

---

Not a medical device. VIGIL reports measured changes; it does not
diagnose.
