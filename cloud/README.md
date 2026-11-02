# VIGIL Cloud

Runs VIGIL for many homes without a Jetson in each one. Each camera sends its
video straight to the cloud server, and the server runs the same pipeline the
Jetson ran: pose, falls, identity, walking measures, alerts.

```
camera (RTMP push) ──► mediamtx :1935 ──► site-home01 (Server.py + activity_monitor.py)
                                     └──► site-home02 ...
iPhone app ──HTTPS──► caddy :443 ──► gateway (accounts, access, proxy, offline alerts) ──► site-*
```

* **One container per camera.** A crash, a full disk or a bad camera in one home
  never touches another. Each has its own database and identity memory.
* **Nothing is exposed but the front door.** Sites listen only on the private
  Docker network. The gateway checks the caregiver's sign-in and which cameras
  they were granted, then talks to the site with that site's private key.
* **Video is not stored.** Frames are analysed in memory and dropped, as on the
  Jetson. MediaMTX recording is off.
* **Monitoring offline is an alert.** If a camera stops sending data for 3
  minutes, everyone on that camera gets an urgent push, every hour until it is
  back, and a "restored" push when it returns.

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
3. **Open the camera port.** VPC network > Firewall > Create rule: ingress,
   targets "all instances", source `0.0.0.0/0`, TCP `1935`.
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

## Adding a camera

```
python3 vigilctl.py add-camera --name "Mohan home - living room" --home "Mohan home" \
    --room "Living room" --tz America/Los_Angeles
```
It prints two things:

* **The RTMP address** to paste into the camera. On an Amcrest or Dahua camera:
  open its web page (its IP address in a browser on the same Wi-Fi), log in,
  Setup > Network > RTMP: Enable, Address Type **Custom**, paste the address, Save.
  Use the sub-stream or main stream at 1280x720, H.264, 10 to 15 fps, about
  1 Mbps. The camera starts sending within a minute.
* **The ntfy topic.** Every caregiver installs the free **ntfy** app and
  subscribes to that topic to get fall alerts on their lock screen.

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
In the app: Settings > Server > **Sign in to VIGIL Cloud**. A person granted
several cameras switches between them from the Today screen.

## Day to day

* Update to the newest code: `bash update_server.sh` (data is kept).
* Logs of one camera: `docker compose logs -f site-home01`, or the files in
  `data/sites/home01/logs/`.
* Back up: `data/` holds every database and identity memory. `config/` holds
  every key. Copy both somewhere private, never to GitHub.

## Before a hospital or a care home

* Written consent from the patient or resident (and roommates), and the
  facility's approval of where video is processed.
* The RTMP link from camera to server is not encrypted. The stream key is long
  and random, but tell the facility, and prefer a camera with RTMPS or SRT if
  they require encryption in transit.
* VIGIL adds to staff checks; it never replaces them. Run the full fall test
  (20 staged falls, 20 everyday movements) on the installed camera first.
