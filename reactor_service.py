"""Reactor X2 video-effects service: real client, with a mock fallback when
no API key is configured.

X2 speaks a persistent streaming session, not per-frame request/reply: the
client publishes a continuous `source` track and the model streams edited
frames back on its own `main_video` track, paced by its own block cadence
rather than by how fast we push. So the interface here is push/pull, not
process-one-get-one: push_frame() feeds the camera in, get_latest_output()
returns whatever the model most recently produced (or None before it has
anything).
"""
import asyncio
import threading

import cv2
import numpy as np
from reactor_sdk import Reactor, ReactorStatus, TrackDirection, TrackKind


class MockReactorX2:
    """Mock Reactor X2 - passthrough: whatever's pushed is what's returned."""

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.prompt = "no effect"
        self.frame_count = 0
        self.reference_image_name = None
        self.connected = False
        self._lock = threading.Lock()
        self._latest_output = None

    async def connect(self):
        print("[Reactor] Connected (mock)")
        self.connected = True

    async def set_prompt(self, prompt):
        self.prompt = prompt
        print(f"[Reactor] Prompt: {prompt}")

    async def set_reference_image(self, filename, save_path):
        self.reference_image_name = filename

    def push_frame(self, jpeg_bytes: bytes):
        with self._lock:
            self._latest_output = jpeg_bytes
        self.frame_count += 1

    def get_latest_output(self):
        with self._lock:
            return self._latest_output

    async def disconnect(self):
        print("[Reactor] Disconnected")
        self.connected = False


class RealReactorX2:
    """Real Reactor X2 client: streams camera frames in on `source`, reads
    edited frames back from `main_video` as the model produces them."""

    def __init__(self, api_key):
        self.api_key = api_key
        self.prompt = ""
        self.frame_count = 0
        self.reference_image_name = None
        self.connected = False
        self.reactor = None
        self.source_track = None
        self._lock = threading.Lock()
        self._latest_output = None

    async def connect(self):
        self.reactor = Reactor(model_name="x2", api_key=self.api_key)

        @self.reactor.on_status(ReactorStatus.READY)
        async def _on_ready(status):
            self.source_track = await self.reactor.publish_track("source")
            output = (
                self.reactor.tracks
                .with_direction(TrackDirection.RECVONLY)
                .with_kind(TrackKind.VIDEO)
                .one()
            )

            @output.on_frame
            def _on_frame(frame):
                self._store_output_frame(frame)

            self.connected = True
            print("[Reactor] X2 session ready")

        await self.reactor.connect()

    def _store_output_frame(self, frame):
        """frame: (H, W, 3) uint8 RGB numpy array from the model."""
        try:
            # np.ascontiguousarray: the RGB->BGR channel reverse below is a
            # negative-stride view: cv2.imencode needs contiguous memory.
            bgr = np.ascontiguousarray(frame[:, :, ::-1])
            ok, buf = cv2.imencode('.jpg', bgr)
            if ok:
                with self._lock:
                    self._latest_output = buf.tobytes()
                self.frame_count += 1
        except Exception as e:
            print(f"[Reactor] frame encode error: {e}")

    async def set_prompt(self, prompt):
        self.prompt = prompt
        resp = await self.reactor.send_command("set_prompt", {"prompt": prompt})
        print(f"[Reactor] set_prompt reply: {resp}")

    async def set_reference_image(self, filename, save_path):
        file_ref = await self.reactor.upload_file(save_path)
        resp = await self.reactor.send_command(
            "set_reference_image", {"reference_image": file_ref}
        )
        print(f"[Reactor] set_reference_image reply: {resp}")
        self.reference_image_name = filename

    def push_frame(self, jpeg_bytes: bytes):
        if self.source_track is None:
            return
        try:
            arr = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if arr is None:
                return
            rgb = np.ascontiguousarray(arr[:, :, ::-1])
            self.source_track.push_frame(rgb)
        except Exception as e:
            print(f"[Reactor] push_frame error: {e}")

    def get_latest_output(self):
        with self._lock:
            return self._latest_output

    async def disconnect(self):
        if self.reactor is not None:
            await self.reactor.disconnect()
        self.connected = False


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
    reactor_service = RealReactorX2(api_key) if api_key else MockReactorX2()
    try:
        _bg.run(reactor_service.connect())
    except Exception as e:
        # Reactor X2 is a bonus feature on top of the rover control app --
        # a bad/unauthorized key or network hiccup here must not take down
        # driving, video, or anything else the rest of app.py depends on.
        print(f"[Reactor] connect failed, Reactor X2 disabled: {e}")
    return reactor_service


def set_prompt(prompt):
    return _bg.run(reactor_service.set_prompt(prompt))


def push_frame(jpeg_bytes: bytes):
    """Feed one camera frame in. Safe to call at whatever rate frames
    arrive -- the mock buffers just the latest; the real client streams
    every one to X2's `source` track."""
    if reactor_service is not None:
        reactor_service.push_frame(jpeg_bytes)


def get_latest_output():
    """The latest processed JPEG frame, or None if nothing's arrived yet
    (e.g. no prompt set, or the model hasn't produced its first block)."""
    if reactor_service is None:
        return None
    return reactor_service.get_latest_output()


def set_reference_image(image_bytes: bytes, filename: str, save_path: str):
    with open(save_path, 'wb') as fh:
        fh.write(image_bytes)
    if reactor_service is not None:
        return _bg.run(reactor_service.set_reference_image(filename, save_path))


def get_health():
    return {
        'reactor_connected': reactor_service.connected if reactor_service else False,
        'prompt': reactor_service.prompt if reactor_service else None,
        'frames_processed': reactor_service.frame_count if reactor_service else 0,
        'reference_image': reactor_service.reference_image_name if reactor_service else None,
    }
