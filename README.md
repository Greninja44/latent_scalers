# UGV Beast Control (`latent_scalers`)

Raspberry Pi control app for a Waveshare **UGV Beast** rover — a fork of
Waveshare's [`ugv_rpi`](https://github.com/waveshareteam/ugv_rpi) that adds a
**Reactor X2** page: a live video-to-video editing view driven by a text prompt.

The Pi runs this app (web UI, camera, CV, strategy). An ESP32 "lower computer"
running [`ugv_base_general`](https://github.com/waveshareteam/ugv_base_general)
handles motors, servos, lights and sensors. They talk JSON over GPIO UART.

---

## Quick start

On a fresh Raspberry Pi (Bookworm, 64-bit):

```bash
git clone https://github.com/Greninja44/latent_scalers.git
cd latent_scalers
sudo ./scripts/setup.sh      # apt packages, UART config, venv, pip install
./scripts/autorun.sh         # install the @reboot cron jobs (no sudo)
sudo reboot
```

After boot the OLED shows the Pi's IP address. Then:

| What | Where |
| --- | --- |
| Main driving UI | `http://<pi-ip>:5000/` |
| Reactor X2 page | `http://<pi-ip>:5000/reactor.html` |
| JupyterLab | `http://<pi-ip>:8888/` |

To run it by hand instead of via cron:

```bash
./ugv-env/bin/python -u app.py     # -u matters; see docs/setup.md
```

Full install notes, environment variables and troubleshooting:
**[docs/setup.md](docs/setup.md)**.

---

## Repository layout

```
app.py               Flask + Socket.IO server: routes, sockets, MJPEG streams,
                     command-line parser, boot sequence. The entry point.
base_ctrl.py         Serial link to the ESP32 — drive, gimbal, lights, OLED,
                     and the feedback reader thread.
cv_ctrl.py           All camera and computer vision: capture, OSD, recording,
                     face/object/colour/gesture/pose detection, line tracking,
                     auto-drive, pan-tilt tracking.
audio_ctrl.py        Sound playback (pygame) and text-to-speech (pyttsx3).
os_info.py           Background thread polling CPU/RAM/temperature/IP/RSSI.
reactor_service.py   Reactor X2 client — the one substantial addition to
                     upstream. Mock passthrough when no API key is set.
config.yaml          Robot type, speeds, CV tuning, and the numeric codes the
                     web UI and server use to talk to each other.
requirements.txt     Pinned Python dependencies.

docs/                Architecture, setup and Reactor X2 documentation.
scripts/             setup.sh, autorun.sh, start_jupyter.sh.
system/              Files setup.sh installs into /etc (ALSA + OAK udev rule).
templates/           The web UI — HTML pages, JS, CSS, fonts, icons. Served
                     both by Jinja (index.html) and as static files.
models/              Pre-trained OpenCV models (MobileNet-SSD, Haar cascade).
sounds/              Startup/connect chimes, and uploaded audio.
```

## Documentation

- **[docs/setup.md](docs/setup.md)** — installing, running, environment
  variables, and the failure modes worth knowing before you debug one.
- **[docs/architecture.md](docs/architecture.md)** — how the two computers
  split the work, the video pipeline, the Socket.IO channels, and how
  `config.yaml`'s numeric codes tie the UI to the server.
- **[docs/reactor-x2.md](docs/reactor-x2.md)** — what X2 does (and, more
  importantly, what it does *not* do), the session lifecycle, the HTTP API,
  and measured latency.

## What is upstream and what is not

Everything except `reactor_service.py`, `templates/reactor.html` and the
Reactor routes in `app.py` is upstream Waveshare code, kept close to original
so upstream fixes remain easy to apply. This fork additionally:

- removes the JupyterLab tutorial notebooks, the AccessPopup hotspot installer
  and the shipped placeholder photos/videos;
- resolves script paths from the repo location instead of hardcoding
  `~/ugv_rpi`, so the checkout can live anywhere.

## License

Upstream `ugv_rpi` code: Copyright (C) 2024
[Waveshare](https://www.waveshare.com/), GNU GPLv3 — see [LICENSE](./LICENSE).
Modifications in this fork are licensed the same way.
