# Architecture

## Two computers

```
        Browser
           |  HTTP (pages, MJPEG)  +  Socket.IO (/json, /ctrl)
           v
  Raspberry Pi  —  app.py                     "upper computer"
    vision, web UI, strategy, Reactor X2
           |
           |  JSON lines over GPIO UART @ 115200
           v
  ESP32 driver board  —  ugv_base_general      "lower computer"
    motors, servos, lights, OLED, IMU, sensors
```

The split is upstream's and worth respecting: anything time-critical or
electrical lives on the ESP32, and the Pi never speaks to a motor directly.
`base_ctrl.BaseController` is the entire Pi-side surface of that link. Every
command is a JSON object with a `"T"` field naming the command, e.g.
`{"T":1,"L":0.5,"R":0.5}` to drive, `{"T":133,"X":pan,"Y":tilt,...}` for the
gimbal. A background reader thread pulls feedback frames back the other way.

`app.py` picks the serial device by Pi model: `/dev/ttyAMA0` on a Pi 5,
`/dev/serial0` on everything older.

## Modules

| File | Responsibility |
| --- | --- |
| `app.py` | Flask + Socket.IO server. Routes, socket handlers, both MJPEG generators, the text command-line parser, and the boot sequence. |
| `base_ctrl.py` | `ReadLine` (buffered serial reader) and `BaseController` — drive, gimbal, lights, OLED, ESP-NOW peers, feedback thread. |
| `cv_ctrl.py` | `OpencvFuncs` — the camera loop plus every CV mode: motion, faces (Haar + MediaPipe), objects (MobileNet-SSD), colour tracking, hand gestures, pose, line following, auto-drive, timelapse, OSD, capture and recording. |
| `audio_ctrl.py` | pygame playback and pyttsx3 speech. Degrades to silent no-ops when no sound card is present. |
| `os_info.py` | `SystemInfo`, a daemon thread polling CPU load/temperature, RAM, IP addresses, WiFi mode and RSSI, and media folder sizes. |
| `reactor_service.py` | The Reactor X2 client. See [reactor-x2.md](reactor-x2.md). |

## Video pipeline

There are two MJPEG streams, and their relationship is the single most
surprising thing in this codebase.

```
camera ──> cvf.frame_process() ──> generate_frames()  ──> /video_feed
                                        │
                                        ├─> latest_frame  (shared, guarded by
                                        │                  a threading.Condition)
                                        └─> reactor_service.push_frame()
                                                     │
                                          Reactor X2 session (async)
                                                     │
                          get_latest_output() <──────┘
                                   │
                       generate_frames_reactor() ──> /video_feed_reactor
```

Two consequences:

- **`generate_frames()` is the only producer.** It is what pulls from the
  camera, what fills `latest_frame`, and what pushes frames into X2. With no
  viewer on `/video_feed`, `/video_feed_reactor` produces nothing at all. The
  Reactor page holds both `<img>` tags open so it works there — any headless
  test must hold `/video_feed` open too.
- **Do not call `cvf.frame_process()` a second time** to get a frame for the
  Reactor stream. It re-reads the camera and has side effects (OSD, recording,
  detection state). That is what `latest_frame` exists to avoid.

`generate_frames_reactor()` deliberately does not reuse the first loop's
wait/notify: X2 is a streaming session, not a per-frame call, so input and
output run at their own independent cadences. It polls for the model's most
recent output and falls back to the raw camera frame when there is none.

MJPEG (`multipart/x-mixed-replace`) must be consumed with an `<img>` tag.
A `<video>` element will not render it.

## Browser ↔ server channels

Three channels, each with a different job:

**Socket.IO `/json`, event `json`** — raw hardware control. The payload is
forwarded verbatim to `base.base_json_ctrl()` and out to the ESP32. This is
what the joystick and the WASD keys use, and what `reactor.html` reuses rather
than inventing its own endpoints.

**Socket.IO `/ctrl`** — the UI channel, in both directions:
- browser → server, event `message`: `{"A": <code>}`, a UI action code from
  `config.yaml`'s `code:` block (`10303` = face detection, `10404` = LED off, …).
- server → browser, event `update`: a telemetry dict keyed by the numeric codes
  in `config.yaml`'s `fb:` block (`106` = CPU load, `112` = battery voltage, …),
  emitted every 5s by `update_data_loop()` and immediately after any action
  that changes state.

**HTTP** — pages, MJPEG streams, media management, audio upload/playback, the
WebRTC `/offer` endpoint, and the Reactor API.

### Why the numeric codes

Both `code:` and `fb:` live in `config.yaml`, and the browser fetches
`/config` and parses it with `js-yaml` rather than hardcoding the numbers. Keep
it that way — the codes are a shared contract, and a client that hardcodes them
breaks silently the moment the file changes.

## HTTP routes

| Route | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Main driving UI (Jinja-rendered `index.html`) |
| `/<path:filename>` | GET | Catch-all serving `templates/` as static files — this is how `reactor.html`, `video.html`, `photo.html` and `settings.html` are reached. They are **not** Jinja-rendered, so `url_for` in those files will not be interpolated. |
| `/config` | GET | Raw `config.yaml`, for the browser to parse |
| `/video_feed` | GET | MJPEG, live camera |
| `/video_feed_reactor` | GET | MJPEG, Reactor X2 output |
| `/offer` | POST | WebRTC SDP offer/answer |
| `/send_command` | POST | Text command line (see below) |
| `/get_photo_names`, `/delete_photo`, `/get_video_names`, `/delete_video`, `/videos/<f>` | | Media gallery |
| `/getAudioFiles`, `/uploadAudio`, `/playAudio`, `/stop_audio` | | Audio |
| `/api/reactor/*` | | See [reactor-x2.md](reactor-x2.md) |

## The text command line

`cmdline_ctrl()` in `app.py` parses the strings typed into the UI's command
box (and the ones `cmd_on_boot()` replays at startup):

| Command | Meaning |
| --- | --- |
| `base -c <json>` | Send raw JSON to the ESP32 |
| `base -r on\|off` | Show received serial frames in the OSD |
| `audio -s <text>` / `-v <0..1>` / `-p <file>` | Speak / set volume / play file |
| `send -a -b`, `send -rm <mac>`, `send -b <msg>` | ESP-NOW peers and broadcast |
| `cv -r [l,l,l] [u,u,u]` / `cv -s <colour>` | Target colour range / named colour |
| `line -r ...` / `line -s <7 floats>` | Line-tracking colour and tuning |
| `video -q <n>` | JPEG quality |
| `track <a1> <a2>` | Pan-tilt tracking parameters |
| `timelapse -s <spd> <time> <interval> <loops>` / `-e` | Start / stop timelapse |
| `s <NN>` | Set chassis + module and persist to `config.yaml` |
| `p <NN>` | Same, without persisting |

Argument parsing is positional and mostly unvalidated — a malformed command
silently `return`s rather than reporting an error.

## Startup order

`app.py`'s `__main__` block, in order: lights on → startup chime → measure
media folders → point the gimbal/arm forward → start `SystemInfo` → start the
telemetry loop → start the ESP32 feedback loop → lights off → `cmd_on_boot()`
(feedback interval, serial flow on, echo off, module select, ESP-NOW setup) →
`init_reactor()` → `socketio.run()`.

`init_reactor()` catches everything. Reactor is an extra on top of a rover
control app, and a bad key or a network hiccup must not take down driving or
video — which it used to, by letting the exception propagate at import time.
