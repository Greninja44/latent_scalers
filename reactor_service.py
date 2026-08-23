"""Mock Reactor X2 video-effects service.

Runs its own background asyncio event loop so the synchronous Flask/MJPEG
code can call into it without paying loop-setup cost per frame. This is
also the integration point for the real (network-based) Reactor X2 client
later, since that client will need real async I/O.
"""
import asyncio
import threading


class MockReactorX2:
    """Mock Reactor X2 - returns each frame unchanged."""

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.prompt = "no effect"
        self.frame_count = 0
        self.reference_image_name = None

    async def connect(self):
        print("[Reactor] Connected (mock)")
        return True

    async def set_prompt(self, prompt):
        self.prompt = prompt
        print(f"[Reactor] Prompt: {prompt}")

    async def process_frame(self, frame: bytes) -> bytes:
        """Mock: return the JPEG frame unchanged. Real X2 will edit it."""
        self.frame_count += 1
        return frame

    async def disconnect(self):
        print("[Reactor] Disconnected")


class _BackgroundLoop:
    """One asyncio event loop on a daemon thread for the process lifetime,
    so sync callers (the MJPEG generator, Flask routes) can submit
    coroutines without blocking on loop setup each call."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, coro, timeout=5):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


_bg = _BackgroundLoop()
reactor_service = None


def init_reactor(api_key=None):
    global reactor_service
    reactor_service = MockReactorX2(api_key)
    _bg.run(reactor_service.connect())
    return reactor_service


def set_prompt(prompt):
    return _bg.run(reactor_service.set_prompt(prompt))


def process_frame_sync(frame: bytes) -> bytes:
    if reactor_service is None:
        return frame
    return _bg.run(reactor_service.process_frame(frame))


def set_reference_image(image_bytes: bytes, filename: str, save_path: str):
    """Store a reference image for later use once the real Reactor X2 API
    (which can do character insertion) is connected. The mock has no image
    model, so this has no visual effect yet -- it's just persisted."""
    with open(save_path, 'wb') as fh:
        fh.write(image_bytes)
    if reactor_service is not None:
        reactor_service.reference_image_name = filename


def get_health():
    return {
        'reactor_connected': reactor_service is not None,
        'prompt': reactor_service.prompt if reactor_service else None,
        'frames_processed': reactor_service.frame_count if reactor_service else 0,
        'reference_image': reactor_service.reference_image_name if reactor_service else None,
    }


# TODO: when the real Reactor X2 API key is ready, replace MockReactorX2
# with a client that streams frames to the real service in process_frame()
# (and does the real connect()/disconnect() handshake). The background
# loop, the sync wrappers, and the Flask routes in app.py stay the same.
