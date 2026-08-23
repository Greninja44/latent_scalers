"""Reactor video-effects service: real client, with a mock fallback when no
API key is configured.

Targets `xmax/x2` -- streaming video-to-video editing guided by a text prompt
plus an optional reference image (character swap/insertion). An earlier attempt
used `reactor/sana-streaming`, which reaches READY but never answers a single
control message (every send_command and publish_track times out at 10s).

X2 speaks a persistent streaming session, not per-frame request/reply: the
client publishes a continuous `source` track and the model streams edited
frames back on `main_video`, paced by its own block cadence (4 frames/block)
rather than by how fast we push. So the interface here is push/pull, not
process-one-get-one: push_frame() feeds the source in, get_latest_output()
returns whatever the model most recently produced. X2 needs no explicit
`start` -- it generates once a prompt and frames are present.

Two things this has to get right, both learned from a silent failure where the
page showed a frozen frame for many minutes while /api/reactor/health happily
reported connected:true:

  * A publish does not survive the session leaving `ready`. The session can
    drop and come back on its own, and when it does the `source` track is
    unpublished, so every push_frame raises INVALID_STATE and no new output is
    ever produced. So publishing is driven off the status handler, not done
    once at connect, and the prompt is re-sent with it.
  * Liveness must be *derived*, never cached. A bool set once at connect stays
    true forever after the session dies. Both `connected` and the freshness of
    the last output frame are computed at read time, and stale output is
    withheld rather than served, so a dead session looks dead.
"""
import asyncio
import threading
import time
from collections import deque

import cv2
import numpy as np
from reactor_sdk import Reactor, ReactorStatus, TrackDirection, TrackKind

MODEL_NAME = "xmax/x2"
SOURCE_TRACK = "source"
READY_TIMEOUT_S = 45
# Hard server-side cap on xmax/x2 set_prompt. Exceeding it fails the whole
# command, so a long prompt silently leaves the previous one in effect.
MAX_PROMPT_CHARS = 1000
# Output older than this is treated as no output at all. X2 delivers in blocks
# with gaps well under a second, so a multi-second gap means the stream has
# stopped, not that it is between blocks.
STALE_AFTER_S = 3.0


class MockReactorX2:
    """Mock Reactor - passthrough: whatever's pushed is what's returned."""

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.prompt = "no effect"
        self.frame_count = 0
        self.reference_image_name = None
        self._lock = threading.Lock()
        self._latest_output = None
        self._last_output_ts = 0.0
        self._connected = False

    @property
    def connected(self):
        return self._connected

    async def connect(self):
        self._connected = True
        print("[Reactor] Connected (mock)")

    async def set_prompt(self, prompt):
        self.prompt = prompt
        print(f"[Reactor] Prompt: {prompt}")

    async def set_reference_image(self, filename, save_path):
        self.reference_image_name = filename

    def push_frame(self, jpeg_bytes: bytes):
        if not self._connected:
            return
        with self._lock:
            self._latest_output = jpeg_bytes
            self._last_output_ts = time.monotonic()
        self.frame_count += 1

    def get_latest_output(self):
        with self._lock:
            if not self._last_output_ts:
                return None
            if time.monotonic() - self._last_output_ts > STALE_AFTER_S:
                return None
            return self._latest_output

    def get_stats(self):
        return {'output_fps': 0.0, 'push_fps': 0.0, 'output_age_s': 0.0,
                'status': 'mock', 'published': self._connected,
                'max_prompt_chars': MAX_PROMPT_CHARS}

    async def disconnect(self):
        self._connected = False
        with self._lock:
            self._latest_output = None
            self._last_output_ts = 0.0
        print("[Reactor] Disconnected (mock)")

    async def reset(self):
        await self.disconnect()
        await self.connect()


class RealReactorModel:
    """Real Reactor client for MODEL_NAME: streams frames in on `source`,
    reads edited frames back from `main_video` as the model produces them."""

    def __init__(self, api_key):
        self.api_key = api_key
        self.prompt = ""
        self.frame_count = 0
        self.reference_image_name = None
        self.reactor = None
        self.source_track = None
        self._lock = threading.Lock()
        self._latest_output = None
        self._last_output_ts = 0.0
        self._output_ts = deque(maxlen=60)
        self._push_ts = deque(maxlen=60)
        self._publish_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Liveness -- all derived at read time, never cached
    # ------------------------------------------------------------------
    @property
    def published(self):
        if self.reactor is None:
            return False
        try:
            return self.reactor.track(SOURCE_TRACK).published
        except Exception:
            return False

    @property
    def status(self):
        return str(self.reactor.status) if self.reactor is not None else "none"

    @property
    def connected(self):
        return (
            self.reactor is not None
            and self.reactor.status == ReactorStatus.READY
            and self.published
        )

    @property
    def output_age(self):
        with self._lock:
            ts = self._last_output_ts
        return time.monotonic() - ts if ts else float('inf')

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------
    async def connect(self):
        self.reactor = Reactor(model_name=MODEL_NAME, api_key=self.api_key)

        # async def, so _fire schedules it onto this loop instead of running it
        # on the FFI control thread it fires from.
        @self.reactor.on_status
        async def _on_status(status):
            print(f"[Reactor] status -> {status}")
            if status == ReactorStatus.READY:
                try:
                    await self._ensure_published()
                except Exception as e:
                    print(f"[Reactor] re-publish after {status} failed: {e}")

        await self.reactor.connect()

        waited = 0.0
        while self.reactor.status != ReactorStatus.READY and waited < READY_TIMEOUT_S:
            await asyncio.sleep(0.25)
            waited += 0.25
        if self.reactor.status != ReactorStatus.READY:
            raise TimeoutError(
                f"{MODEL_NAME} did not reach ready within {READY_TIMEOUT_S}s "
                f"(status={self.reactor.status})"
            )

        output = (
            self.reactor.tracks
            .with_direction(TrackDirection.RECVONLY)
            .with_kind(TrackKind.VIDEO)
            .one()
        )

        @output.on_frame
        def _on_frame(frame):
            self._store_output_frame(frame)

        # The status handler above has very likely done this already; the lock
        # and the published check make it idempotent.
        await self._ensure_published()
        print(f"[Reactor] {MODEL_NAME} session ready")

    async def _ensure_published(self):
        """Publish `source` if it isn't, and restore the prompt with it.

        Called both at connect and on every return to READY, because a publish
        does not survive the session leaving ready and neither does the prompt.
        """
        async with self._publish_lock:
            if self.published:
                return
            self.source_track = await self.reactor.publish_track(SOURCE_TRACK)
            print(f"[Reactor] published '{SOURCE_TRACK}'")
            if self.prompt:
                await self.reactor.send_command("set_prompt", {"prompt": self.prompt})
                print("[Reactor] prompt restored after publish")

    def _store_output_frame(self, frame):
        """frame: (H, W, 3) uint8 RGB numpy array from the model."""
        try:
            # np.ascontiguousarray: the RGB->BGR channel reverse below is a
            # negative-stride view: cv2.imencode needs contiguous memory.
            bgr = np.ascontiguousarray(frame[:, :, ::-1])
            ok, buf = cv2.imencode('.jpg', bgr)
            if ok:
                now = time.monotonic()
                with self._lock:
                    self._latest_output = buf.tobytes()
                    self._last_output_ts = now
                    self._output_ts.append(now)
                self.frame_count += 1
        except Exception as e:
            print(f"[Reactor] frame encode error: {e}")

    async def set_prompt(self, prompt):
        # Checked here so an over-long prompt is a clear, immediate error
        # rather than an [invalid_command] round trip, and so self.prompt is
        # never left holding something the model rejected.
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(
                f"prompt is {len(prompt)} characters; {MODEL_NAME} accepts at "
                f"most {MAX_PROMPT_CHARS}"
            )
        # Explicit, so a prompt sent while the session is down reports that
        # plainly instead of an AttributeError on a None reactor.
        if self.reactor is None:
            raise RuntimeError("Reactor is disconnected -- press Connect first")
        resp = await self.reactor.send_command("set_prompt", {"prompt": prompt})
        # Only after the model has accepted it: assigning first meant a
        # rejected prompt still showed up in /api/reactor/health as though it
        # had taken effect.
        self.prompt = prompt
        print(f"[Reactor] set_prompt ({len(prompt)} chars) reply: {resp}")

    async def set_reference_image(self, filename, save_path):
        if self.reactor is None:
            raise RuntimeError("Reactor is disconnected -- press Connect first")
        ref = await self.reactor.upload_file(save_path)
        resp = await self.reactor.send_command(
            "set_reference_image", {"reference_image": ref}
        )
        self.reference_image_name = filename
        print(f"[Reactor] set_reference_image reply: {resp}")

    def push_frame(self, jpeg_bytes: bytes):
        # Checked rather than caught: after the session drops this is called
        # ~24x/s, and letting each one raise floods the log with identical
        # INVALID_STATE tracebacks.
        if not self.published:
            return
        try:
            arr = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if arr is None:
                return
            rgb = np.ascontiguousarray(arr[:, :, ::-1])
            self.source_track.push_frame(rgb)
            with self._lock:
                self._push_ts.append(time.monotonic())
        except Exception as e:
            print(f"[Reactor] push_frame error: {e}")

    def get_latest_output(self):
        """The most recent edited frame, or None if the model has produced
        nothing recently. Withheld rather than repeated: serving the last good
        frame forever is what made a dead session look like a working one."""
        with self._lock:
            if not self._last_output_ts:
                return None
            if time.monotonic() - self._last_output_ts > STALE_AFTER_S:
                return None
            return self._latest_output

    @staticmethod
    def _rate(ts_deque):
        if len(ts_deque) < 2:
            return 0.0
        span = ts_deque[-1] - ts_deque[0]
        return round((len(ts_deque) - 1) / span, 1) if span > 0 else 0.0

    def get_stats(self):
        with self._lock:
            out_fps = self._rate(self._output_ts)
            push_fps = self._rate(self._push_ts)
        age = self.output_age
        return {
            'output_fps': out_fps,
            'push_fps': push_fps,
            'output_age_s': round(age, 2) if age != float('inf') else None,
            'status': self.status,
            'published': self.published,
            'max_prompt_chars': MAX_PROMPT_CHARS,
        }

    async def disconnect(self):
        # Cleared before awaiting the teardown: push_frame and the MJPEG
        # generator run on other threads and must stop touching the session
        # immediately, not once the await returns.
        r, self.reactor = self.reactor, None
        self.source_track = None
        with self._lock:
            self._latest_output = None
            self._last_output_ts = 0.0
            self._output_ts.clear()
            self._push_ts.clear()
        if r is not None:
            try:
                await r.disconnect()
            except Exception as e:
                print(f"[Reactor] disconnect error (ignored): {e}")
        print("[Reactor] disconnected")

    async def reset(self):
        """Tear the session down and build a fresh one, restoring the prompt.

        This is the only lever available on the latency drift: content delay
        climbs the longer a session streams (measured ~6s -> ~9s over 90s), and
        a new session starts from an empty pipeline and a cleared KV/VAE cache.
        """
        prompt = self.prompt
        await self.disconnect()
        await self.connect()
        if prompt:
            await self.set_prompt(prompt)
        print("[Reactor] reset complete")


class _BackgroundLoop:
    """One asyncio event loop on a daemon thread for the process lifetime,
    so sync callers (the MJPEG generator, Flask routes) can submit
    coroutines without blocking on loop setup each call."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


_bg = _BackgroundLoop()
reactor_service = None


def init_reactor(api_key=None):
    global reactor_service
    reactor_service = RealReactorModel(api_key) if api_key else MockReactorX2()
    try:
        # Generous timeout: connect() covers session creation, the WebRTC
        # handshake and the publish round trip, which together run past the
        # default 15s on a cold model.
        _bg.run(reactor_service.connect(), timeout=READY_TIMEOUT_S + 30)
    except Exception as e:
        # Reactor is a bonus feature on top of the rover control app -- a
        # bad/unauthorized key or network hiccup here must not take down
        # driving, video, or anything else the rest of app.py depends on.
        print(f"[Reactor] connect failed, Reactor disabled: {e}")
    return reactor_service


def set_prompt(prompt):
    return _bg.run(reactor_service.set_prompt(prompt))


def reset():
    """Rebuild the session from scratch, keeping the current prompt."""
    return _bg.run(reactor_service.reset(), timeout=READY_TIMEOUT_S + 60)


def disconnect():
    """Drop the session. Frees the model slot; the stream falls back to the
    raw camera until connect() is called again."""
    return _bg.run(reactor_service.disconnect(), timeout=30)


def connect():
    """Bring a dropped session back up."""
    return _bg.run(reactor_service.connect(), timeout=READY_TIMEOUT_S + 30)


def push_frame(jpeg_bytes: bytes):
    """Feed one source frame in. Safe to call at whatever rate frames
    arrive -- the mock buffers just the latest; the real client streams
    every one to the model's `source` track."""
    if reactor_service is not None:
        reactor_service.push_frame(jpeg_bytes)


def get_latest_output():
    """The latest processed JPEG frame, or None if nothing recent (the model
    hasn't produced its first block, or the session has stopped producing)."""
    if reactor_service is None:
        return None
    return reactor_service.get_latest_output()


def set_reference_image(image_bytes: bytes, filename: str, save_path: str):
    with open(save_path, 'wb') as fh:
        fh.write(image_bytes)
    if reactor_service is not None:
        # Upload + command round trip, so not the default 15s.
        return _bg.run(reactor_service.set_reference_image(filename, save_path), timeout=60)


def get_health():
    if reactor_service is None:
        return {'reactor_connected': False, 'prompt': None,
                'frames_processed': 0, 'reference_image': None}
    health = {
        'reactor_connected': reactor_service.connected,
        'prompt': reactor_service.prompt,
        'frames_processed': reactor_service.frame_count,
        'reference_image': reactor_service.reference_image_name,
    }
    health.update(reactor_service.get_stats())
    return health
