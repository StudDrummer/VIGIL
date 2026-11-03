# VIGIL relay box

A Raspberry Pi Zero 2 W next to a Tapo camera. It finds the camera on the home
Wi-Fi and forwards its video, encrypted, to VIGIL Cloud. No AI runs on it and it
stores no video. Everything that can fail is retried forever; a hardware
watchdog restarts it if it ever hangs, and it restarts itself every Sunday at
4:00.

The green light on the board is the only thing a family needs to read:

| Light | Meaning | What to do |
| --- | --- | --- |
| Steady on | Sending video to VIGIL. | Nothing. |
| Slow blink (1 s) | Cannot find the camera. | Check the camera is plugged in and on the same Wi-Fi. |
| Fast blink | Camera found, no internet to VIGIL. | Check the home internet. |
| Two quick blinks, pause | Does not know this Wi-Fi. | Join **VIGIL-Setup** on a phone (password `vigilsetup`), pick the home Wi-Fi. |

## Parts per camera

* Tapo C225 (or any Tapo with RTSP).
* Raspberry Pi Zero 2 W (with pre-soldered header not needed).
* microSD card, 16 or 32 GB, A1 rated, a known brand (SanDisk High Endurance or Samsung PRO Endurance).
* Raspberry Pi official 5.1 V 2.5 A micro-USB power supply (works on 100 to 240 V).
* A case.
* For India: a plug adapter (Type D/M or a universal one) for the Tapo and Pi supplies.

## Preparing a box (at home, before it ships)

1. **Cloud first.** On the server: `python3 vigilctl.py add-camera --name "..." --home "..." --tz Asia/Kolkata`
   (add `--hospital` for a hospital bed). Copy `config/relay_homeXX.env` to the Mac.
2. **Tapo camera account.** Set the camera up in the Tapo app on your Wi-Fi. Then
   Tapo app > camera > Settings > Advanced settings > **Camera Account**: make a
   username and password (for example `vigil` and a 12-character password). Write them down.
3. **Flash the card.** Raspberry Pi Imager > Raspberry Pi Zero 2 W >
   Raspberry Pi OS Lite (64-bit). Edit settings: hostname `vigil-homeXX`,
   username `vigil`, a password, your Wi-Fi, time zone, and turn on SSH.
4. **Copy and install.** Boot the Pi, then from the Mac:
   ```
   scp -r relay relay_homeXX.env vigil@vigil-homeXX.local:
   ssh vigil@vigil-homeXX.local
   sudo bash relay/install.sh relay_homeXX.env
   ```
   It asks for the Tapo camera account, a Tailscale auth key (recommended: lets
   you fix the box from Washington; make one at login.tailscale.com > Settings >
   Keys, reusable, no expiry) and, if you know it, the Wi-Fi of the home it is
   going to.
5. **Check.** Within a minute the light goes steady and
   `cat /var/lib/vigil-relay/status.json` says `"state": "sending"`. In the app
   the camera shows Online and the live view works.
6. **Test the Wi-Fi setup.** Turn your router's Wi-Fi off (or move the box out of
   range) and wait 2 minutes: the light double-blinks and **VIGIL-Setup** appears.
   Join it, pick a network, check the light goes steady again.
7. **Burn in.** Leave it running 3 days. Check the app each day.
8. **Print the card.** `python3 vigilctl.py card homeXX --tapo-user vigil --tapo-pass '...' --app-link <TestFlight link> --contact <your email>`
   writes `config/card_homeXX.html`. Open it, print it, put it in the box.

## Fixing a box from far away

With Tailscale: `ssh vigil@vigil-relay-homeXX` from any computer on your
Tailscale account. Then:

* `journalctl -u vigil-relay -n 50` what it has been doing
* `cat /var/lib/vigil-relay/status.json` its state right now
* `sudo systemctl restart vigil-relay` start it again
* `sudo nano /etc/vigil-relay.env` change settings (for example `VIGIL_TAPO_IP=` if the scan cannot find the camera)

## Settings in /etc/vigil-relay.env

| Setting | Meaning |
| --- | --- |
| `VIGIL_CAMERA_ID`, `VIGIL_CLOUD_HOST`, `VIGIL_STREAM_USER`, `VIGIL_STREAM_PASS`, `VIGIL_SRT_PASSPHRASE`, `VIGIL_SRT_PORT` | From the cloud. |
| `VIGIL_RTMP_FALLBACK` | RTMP address used when SRT is blocked. Empty: never fall back (always encrypted). |
| `VIGIL_TAPO_USER`, `VIGIL_TAPO_PASS` | The Tapo camera account. |
| `VIGIL_TAPO_IP` | Optional: the camera's address, skips the scan. |
| `VIGIL_TAPO_STREAM` | `stream1` (high quality, default) or `stream2` (lower, for slow internet). |
| `VIGIL_SOURCE_URL` | Optional: any other RTSP camera or a test video file instead of a Tapo. |
