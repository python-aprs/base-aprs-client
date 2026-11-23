"""An in-memory stand-in for the aprs async protocols."""

import asyncio

from aprs import APRSFrame


class FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed


class FakeProtocol:
    """Records written frames; ``feed()`` delivers frames, ``drop()`` ends the connection."""

    def __init__(self):
        self.written: list[str] = []
        self.frames: asyncio.Queue = asyncio.Queue()
        self.transport = FakeTransport()

    def write(self, frame):
        self.written.append(str(frame))

    async def read(self):
        while True:
            frame = await self.frames.get()
            if frame is None:
                return
            yield frame

    def feed(self, tnc2: str) -> None:
        self.frames.put_nowait(APRSFrame.from_str(tnc2))

    def drop(self) -> None:
        self.frames.put_nowait(None)


async def settle(n: int = 5) -> None:
    """Let the client's reader task process what was fed."""
    for _ in range(n):
        await asyncio.sleep(0)
