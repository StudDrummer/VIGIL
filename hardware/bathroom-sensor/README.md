# VIGIL bathroom sensor

A Seeed MR60FDA2 60 GHz radar kit with a XIAO ESP32-C6 (about $25). It never makes an
image. When it reports a fall, VIGIL sends the usual fall alert: "The bathroom sensor
reported a possible fall". It is its own device on the home Wi-Fi, not part of the VIGIL box.

## Set one up

1. On the VIGIL server: `python3 cloud/vigilctl.py sensor <camera id>`. It writes
   `config/sensor_<camera>_secrets.yaml` with the address and key for that camera.
2. Copy that file here as `secrets.yaml` (never commit it) and fill in the home Wi-Fi.
3. `pip install esphome`, plug the kit in by USB-C, then `esphome run vigil-bathroom.yaml`.
   (The config passes `esphome config` on ESPHome 2026.10.)
4. Mount it on the ceiling or high on the wall, 2.2-3 m up, radar side facing the room,
   within about 2 m of the toilet and shower.
5. Set "Install height" to the real height (ESPHome web page or Home Assistant).
6. Test: sit on the toilet, run the shower, walk in and out (none should alert), then lie
   down slowly on a mat (should alert once, and "clear" when you get up).

If its Wi-Fi is wrong it opens a hotspot called `VIGIL-sensor-setup` to fix it.

## What it sends

`POST https://<cloud>/relay/<camera>/sensor` with header `X-Relay-Key` and
`{"sensor_id", "room", "kind": "radar", "event": "fall" | "clear" | "heartbeat"}`,
a heartbeat every 5 minutes.

## Things to know

- **Water:** Seeed's guide says to avoid flowing water in view. Aim it so the shower stream
  is not the main thing it sees, and test with the shower running.
- **Steam:** the board is bare, so it goes in the VIGIL puck (`hardware/vigil_sensor.py`):
  sealed face, nothing opening downward, breathing gap at the ceiling. Seeed's own case
  (the "mmWave 3D Case" STL on its wiki) is a quick fallback.
- **Power:** it needs constant 5 V USB-C. Most bathrooms have an outlet by the mirror, not
  the ceiling: use a flat USB-C cable along the wall, or mount it high on the wall above the
  outlet instead of on the ceiling.

## What to buy (one bathroom)

| Item | Why | About |
| --- | --- | --- |
| Seeed Studio MR60FDA2 60 GHz Fall Detection Sensor Kit with XIAO ESP32C6 (Seeed 114993388) | The sensor | $25 |
| USB-A to USB-C cable, 90 degree (right-angle) USB-C end, 3 m / 10 ft | Fits inside the puck and reaches an outlet | $10 |
| 5 V USB wall charger, 1 A or more | Power | $8 (or one you have) |
| M2 x 6 mm self-tapping (thread-forming) screws for plastic, 2 | Hold the kit in the puck | $8 for a small kit |
| No. 6 x 1 in flat-head screws and drywall anchors, 2 each | Ceiling mount (or large 3M Command strips) | $5 |
| Adhesive cable clips, white | Run the cable tidily to the outlet | $5 |
