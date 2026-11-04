# VIGIL Cloud

Runs VIGIL for many homes without a Jetson in each one. In each room a Tapo
camera and a small VIGIL relay box (a Raspberry Pi, see `relay/`) send the
video to the cloud server, and the server runs the same pipeline the Jetson
ran: pose, falls, identity, walking measures, alerts.

```
Tapo camera ─RTSP─► relay box ─SRT, encrypted─► mediamtx :8890/udp ─► site-home01 (Server.py + activity_monitor.py)
                              (RTMP :1935 if SRT is blocked)       └─► site-home02 ...
iPhone app ──HTTPS──► caddy :443 ──► gateway (accounts, access, proxy, alert emails, offline alerts) ──► site-*
```

* **One container per camera.** A crash, a full disk or a bad camera in one home
  never touches another. Each has its own database and identity memory.
* **Nothing is exposed but the front door.** Sites listen only on the private
  Docker network. The gateway checks the caregiver's sign-in and which cameras
  they were granted, then talks to the site with that site's private key.
* **Video is not stored.** Frames are analysed in memory and dropped, as on the
  Jetson. MediaMTX recording is off.
* **Monitoring offline is an alert.** If a camera stops sending data for 3
  minutes, everyone on that camera gets an urgent push and an email, every hour
  until it is back, and a "restored" message when it returns.
* **Families make their own accounts.** In the app they create an account
  (email and password), then type the setup code printed on the camera's card.
  The first account to add a camera becomes its owner; family who add the same
  code later become caregivers. Each person can turn alert emails on or off.

## What it costs to run

The pose model needs about 2 CPU cores per camera for 6 to 9 frames a second.

| Cameras | Google Cloud machine | Approx. per month |
| --- | --- | --- |
| 1 to 3 | e2-standard-8 (8 vCPU, 32 GB) | about $200 |
| up to 7 | e2-standard-16 | about $400 |
| up to 16 | e2-standard-32 | about $800 |

New Google Cloud accounts get free credit. Turn on a budget alert.

## Setting it up (about 45 minutes, once)

1. **Google Cloud account.** console.cloud.google.com, create a project, turn on
   billing (the free credit covers the start).
2. **Make the server.** Compute Engine > VM instances > Create:
   * Machine: `e2-standard-8` for the family trial (resize later).
   * Boot disk: Ubuntu 24.04 LTS, 100 GB.
   * Firewall: tick "Allow HTTP" and "Allow HTTPS".
   * Networking > External IP: reserve a **static** address.
3. **Open the camera ports.** VPC network > Firewall > Create rule: ingress,
   targets "all instances", source `0.0.0.0/0`, **UDP `8890`** (SRT, the
   encrypted video from relay boxes) and **TCP `1935`** (RTMP fallback).
4. **Point your domain at it.** Where vigilhealth.me's DNS is managed, add an
   `A` record: name `api`, value = the static IP. (The website is unaffected.)
5. **Install.** Click "SSH" next to the VM, then:
   ```
   git clone https://github.com/StudDrummer/VIGIL.git
   cd VIGIL/cloud
   bash install_server.sh api.vigilhealth.me you@example.com
   ```
   It prints your admin password. Log out of SSH and back in once (Docker group).
6. **Check:** `curl https://api.vigilhealth.me/health` shows `"status":"ok"`.
7. **Email.** Edit `config/email.env` (made by the install) with the SMTP
   account of the VIGIL email:
   ```
   VIGIL_SMTP_HOST=smtp.gmail.com        # or your provider's SMTP server
   VIGIL_SMTP_PORT=587                   # 465 also works (SSL)
   VIGIL_SMTP_USER=alerts@vigilhealth.me
   VIGIL_SMTP_PASSWORD=<an app password, not the normal password>
   VIGIL_SMTP_FROM=VIGIL <alerts@vigilhealth.me>
   ```
   For Gmail or Google Workspace, turn on 2-step verification for that account
   and create an "App password" (myaccount.google.com > Security). Then
   `python3 vigilctl.py apply`, sign in to the app and tap Settings > Account >
   **Send me a test email**. Without this file filled in, accounts still work,
   but there are no welcome, password-reset or alert emails.

## Adding a camera

```
python3 vigilctl.py add-camera --name "Mohan home - living room" --home "Mohan home" \
    --room "Living room" --tz America/Los_Angeles
```
It prints:

* **The setup code** (like `K7Q2-M9XD`). Print it on the card that goes in the
  box. The family types it in the app after making an account; nothing else
  links them to the camera. `camera-url home01` shows it again.
* **The relay settings file**, `config/relay_home01.env`. Copy it to the Mac and
  use it when preparing that camera's relay box (`relay/README.md`). It holds the
  camera's private stream password and encryption key: never put it on GitHub.
* **The ntfy topic.** Optional lock-screen pushes: a caregiver installs the
  free **ntfy** app and subscribes to that topic. Alert emails need nothing.

A camera that can push RTMP by itself (Amcrest, Dahua): `camera-url home01`
also prints the RTMP address to paste in its Setup > Network > RTMP page.

Face matching (one of five cues for telling people apart, see the main
README) is on by default. Tell the family or facility; turn it off for a
camera with `--no-face` when adding it, or later with
`python3 vigilctl.py edit-camera home01 --face off`.

For a hospital bed, add `--hospital` (bed-exit alerts at any hour, not only at
night). The on-the-floor alert is on by default after 60 s; change it with
`--floor-alert-s 90`, or `0` to turn it off.

A camera that cannot push but whose RTSP address the server can reach:
`--pull-url rtsp://user:pass@host:554/stream`.

## Accounts

```
python3 vigilctl.py add-user --email mom@example.com --name "Mom"     # prints a password
python3 vigilctl.py grant --email mom@example.com --camera home01
python3 vigilctl.py grant --email mom@example.com --camera home01 --revoke
python3 vigilctl.py reset-password --email mom@example.com
python3 vigilctl.py users
python3 vigilctl.py cameras
```
Normally nobody needs these: people sign up in the app and add a camera with
its setup code. `add-user` and `grant` are for adding someone by hand. A person
with several cameras switches between them from the Today screen (chips under
the date) or Settings > Account.

### Several cameras in one home

Each camera is its own site with its own people memory, bed and couch areas,
and alerts. Add one per room with the same `--home` name, and give the family
each card's code (or add them all to one account). Teach each person once on
each camera: a name taught on the living-room camera is not yet known to the
bedroom camera.

## Day to day

* Update to the newest code: `bash update_server.sh` (data is kept).
* Logs of one camera: `docker compose logs -f site-home01`, or the files in
  `data/sites/home01/logs/`.
* Back up: `data/` holds every database and identity memory. `config/` holds
  every key. Copy both somewhere private, never to GitHub.

## Before a hospital or a care home

* Written consent from the patient or resident (and roommates), and the
  facility's approval of where video is processed.
* Video from a relay box to the server is encrypted (SRT, AES-128, a separate
  key per camera). If the network blocks UDP, the box falls back to RTMP, which
  is not encrypted, and tries SRT again every hour; the relay log says which is
  in use. If the facility requires encryption at all times, set
  `VIGIL_RTMP_FALLBACK=` (empty) in the box's `/etc/vigil-relay.env`.
* Hospital Wi-Fi often has a sign-in page ("captive portal") that a relay box
  cannot click through. Ask IT to allow the box by its MAC address, or bring a
  mobile hotspot (for example a Jio 4G router) for the camera and box.
* VIGIL adds to staff checks; it never replaces them. Run the full fall test
  (20 staged falls, 20 everyday movements) on the installed camera first.
