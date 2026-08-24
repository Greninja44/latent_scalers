# Setup and operation

## Requirements

- Raspberry Pi 4 or 5 running Raspberry Pi OS **Bookworm, 64-bit**
- A UGV Beast (or UGV Rover / RaspRover) chassis with its ESP32 driver board
- Camera, and optionally a USB sound card and an OAK depth camera

## Install

```bash
git clone https://github.com/Greninja44/latent_scalers.git
cd latent_scalers
sudo ./scripts/setup.sh
```

`scripts/setup.sh` must run with `sudo`, and resolves every path from its own
location — the repo can be cloned anywhere under any name. It:

1. strips the serial console from `cmdline.txt` and enables `dtparam=uart0`,
   so the GPIO UART belongs to this app and not to a login shell;
2. adds `dtoverlay=disable-bt` and disables `hciuart`/`bluetooth`, because on
   the Pi the Bluetooth modem otherwise owns the good UART;
3. installs apt packages (`python3-opencv`, `libcamera-dev`, `portaudio19-dev`,
   `libopenblas-dev`, `espeak`, …);
4. creates `ugv-env/` with `--system-site-packages` — **required**, since
   `python3-opencv`, `libcamera` and `picamera2` come from apt and are not
   pip-installable here — and installs `requirements.txt` into it;
5. adds the login user to the `dialout` group (serial port access);
6. copies `system/asound.conf` to `/etc/asound.conf` and
   `system/99-dai.rules` to `/etc/udev/rules.d/`.

Pass `-i` / `--index` to switch apt and pip to the Tsinghua mirrors, which is
much faster from mainland China and slower from everywhere else.

Then install the boot-time jobs — **without** sudo, since they go into the
login user's crontab:

```bash
./scripts/autorun.sh
sudo reboot
```

That writes two `@reboot` cron lines (the app, and JupyterLab via
`scripts/start_jupyter.sh`) and disables JupyterLab's token/password.

> `setup.sh` changes `/boot/firmware/config.txt`, `cmdline.txt` and
> `/etc/asound.conf`. It backs up neither, and `asound.conf` hardcodes ALSA
> card 3. If the Pi has other audio hardware, check `aplay -l` and edit
> `system/asound.conf` before running setup.

## Running it by hand

```bash
./ugv-env/bin/python -u app.py
```

Use `-u`. Without it Python block-buffers stdout when it is redirected to a
file, so `[Reactor]` lines and tracebacks stay invisible for minutes —
**an empty log is not evidence that nothing happened.** `scripts/autorun.sh`
already adds `-u` to the cron line.

`app.py` binds `0.0.0.0:5000`. Only one process can hold the port; a second
instance fails to bind, and the app does not reliably die on `SIGTERM`, so
verify with `ss -ltnp | grep :5000` and use `kill -9` if one lingers.

## Environment variables

| Variable | Effect |
| --- | --- |
| `REACTOR_API_KEY` | Enables the real Reactor X2 client. Unset → a passthrough mock runs instead, and the Reactor page shows the unedited camera. |

**A cron- or script-launched app does not see `~/.bashrc`.** A non-interactive
shell returns out of `.bashrc` before any `export` runs, so a key that works
when you start the app by hand silently vanishes when a script starts it —
X2 falls back to the mock with nothing in the log pointing at the shell. Put
the key somewhere the launcher explicitly sources, or set it in the cron line.

## Selecting the robot

`config.yaml`'s `base_config.main_type` / `module_type` decide which chassis
and module the app drives. Change them from the web UI's command line with
`s <NN>`, which rewrites `config.yaml` and re-applies immediately:

- first digit — chassis: `1` RaspRover, `2` UGV Rover, `3` UGV Beast
- second digit — module: `0` none, `1` RoArm-M2, `2` Camera pan-tilt

So `s 30` is a UGV Beast with no module (the default here), `s 32` the same
chassis with a pan-tilt camera. Setting the chassis also sets its speed
limits, which is why editing `main_type` in the file by hand is not equivalent.

## Networking

The OLED shows the Ethernet and WiFi addresses on its first two lines and
`F/J:5000/8888` — Flask and Jupyter's ports — on the third. Upstream's
AccessPopup hotspot fallback is **not** included in this fork; if the Pi
cannot reach a known network it will simply not be reachable.

## Troubleshooting

**Nothing moves, no OLED text.** The ESP32 link is down. `app.py` picks
`/dev/ttyAMA0` on a Pi 5 and `/dev/serial0` otherwise. Confirm the port
exists, that the login user is in `dialout` (`groups`), and that setup's
`cmdline.txt` edit survived — a serial console on that UART will fight the app
for every byte.

**Video is black or the app dies at startup.** `cv_ctrl.py` opens the camera at
import time. Check `libcamera-hello` works first; the app cannot start without
a camera.

**No audio.** `audio_ctrl` prints `audio usb not connected` and then silently
no-ops every playback call — it never raises. Check `aplay -l` against the card
number in `/etc/asound.conf`.

**The Reactor page shows plain camera video.** Either no `REACTOR_API_KEY`
(the mock is a passthrough), or the session is down. `GET
/api/reactor/health` distinguishes them; see
[reactor-x2.md](reactor-x2.md).
