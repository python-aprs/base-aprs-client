import asyncio
import socket
import threading
import traceback

import attrs
import pytest

from aprs import TCP
from base_aprs_client import Client


@attrs.define(eq=False)
class APRSISSimulator(threading.Thread):
    """Simulate APRS-IS for unit testing."""

    host = attrs.field(default="127.0.0.1")
    port = attrs.field(default=14588)
    server = attrs.field(default=False)
    writers = attrs.field(factory=list)

    def __attrs_post_init__(self):
        super().__init__()

    def forward(self, writer, addr, message):
        if not isinstance(message, bytes):
            message = f"# {addr!r}: {message!r}\n".encode()
        for w in self.writers:
            if w != writer:
                w.write(message)

    def close(self, message=None):
        self.forward(None, "Server", message or "Shutdown requested")
        for c in self.writers:
            if c:
                c.close()
        self.server.close()

    async def handle(self, reader, writer):
        addr = writer.get_extra_info("peername")
        message = f"{addr!r} is connected !!!!"
        print(message)
        self.forward(writer, addr, message)
        while True:
            data = await reader.read(256)
            if not data:  # EOF
                break
            data, found_comment, comment = data.partition(b"#")
            self.forward(writer, addr, data)
            await writer.drain()
            message = data.decode().strip()
            if message == "exit":
                message = f"{addr!r} wants to close the connection."
                print(message)
                self.forward(writer, "Server", message)
                break

    async def _handle(self, reader, writer):
        self.writers.append(writer)
        try:
            await self.handle(reader, writer)
        except Exception:
            traceback.print_exc()
        finally:
            self.writers.remove(writer)
            writer.close()

    async def _run(self):
        self.server = await asyncio.start_server(
            self._handle,
            self.host,
            self.port,
        )
        addr = self.server.sockets[0].getsockname()
        print(f"Serving on {addr}")
        async with self.server:
            await self.server.start_serving()
            while self.server.sockets:
                await asyncio.sleep(1)

    def run(self):
        asyncio.run(self._run())


def socket_ping(ip, port, timeout=0.1):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        s.close()
        return True
    except socket.error:
        return False


@pytest.fixture
def server():
    s = APRSISSimulator()
    s.start()
    # busy loop until the socket is open
    while not socket_ping(s.host, s.port):
        pass
    yield s
    if s.server:
        s.close()
    s.join()


@pytest.fixture
def client1(server):
    with TCP(
        host="127.0.0.1",
        port=server.port,
    ) as sync_frame_io:
        yield Client(
            mycall="CLIENT1",
            sync_frame_io=sync_frame_io,
        )


@pytest.fixture
def client2(server):
    with TCP(
        host="127.0.0.1",
        port=server.port,
    ) as sync_frame_io:
        yield Client(
            mycall="CLIENT2",
            sync_frame_io=sync_frame_io,
        )
