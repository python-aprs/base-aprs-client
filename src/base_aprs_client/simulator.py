import asyncio
import logging
import socket
import threading
import time

import attrs


logger = logging.getLogger(__name__)


SERVER_BRINGUP_TIMEOUT = 10


@attrs.define(eq=False)
class APRSISSimulator(threading.Thread):
    """
    Miniature APRS-IS-like server for testing.


    from aprs import TCP
    from base_aprs_client import APRSISSimulator, Client


    with APRSISSimulator() as sim:
        with (
            TCP(
                host="127.0.0.1",
                port=server.port,
            ),
            TCP(
                host="127.0.0.1",
                port=server.port,
            ),
        ) as (c1_transport, c2_transport):
            client1 = Client("N0CALL-1", c1_transport)
            client2 = Client("N0CALL-2", c2_transport)

            client1.send_status("Foobar")
            print(client2.read(min_frames=1))
    """

    host = attrs.field(default="127.0.0.1")
    port = attrs.field(default=14588)
    server = attrs.field(default=False)
    writers = attrs.field(factory=list)
    _stop_evt = attrs.field(factory=threading.Event)

    def __attrs_post_init__(self):
        super().__init__()

    def _forward(self, writer, addr, message):
        if not isinstance(message, bytes):
            message = f"# {addr!r}: {message!r}\n".encode()
        for w in self.writers:
            if w != writer:
                w.write(message)

    def _close(self, message=None):
        self._forward(None, "Server", message or "Shutdown requested")
        for c in self.writers:
            if c:
                c.close()
        self.server.close()

    async def _handle(self, reader, writer):
        addr = writer.get_extra_info("peername")
        message = f"{addr!r} is connected !!!!"
        logger.info(message)
        self._forward(writer, addr, message)
        while True:
            data = await reader.read(4096)
            if not data:  # EOF
                break
            data, found_comment, comment = data.partition(b"#")
            message = data.decode().strip()
            if message.startswith("user "):
                _, _, username = message.partition(" ")
                logger.info("{} logs in",format(username.strip()))
                continue
            if message == "exit":
                message = f"{addr!r} wants to close the connection."
                logger.info(message)
                self._forward(writer, "Server", message)
                break
            self._forward(writer, addr, data)
            await writer.drain()

    async def _handle_connection(self, reader, writer):
        self.writers.append(writer)
        try:
            await self._handle(reader, writer)
        except Exception:
            logger.exception("Exception handling message.")
        finally:
            self.writers.remove(writer)
            writer.close()

    async def _run(self):
        self.server = await asyncio.start_server(
            self._handle_connection,
            self.host,
            self.port,
        )
        addr = self.server.sockets[0].getsockname()
        print(f"Serving on {addr}")
        async with self.server:
            await self.server.start_serving()
            while self.server.sockets and not self._stop_evt.is_set():
                await asyncio.sleep(1)
            self.close()

    def run(self):
        asyncio.run(self._run())

    def __enter__(self):
        self.start()
        # busy loop until the socket is open
        timeout = time.time() + SERVER_BRINGUP_TIMEOUT
        while not socket_ping(self.host, self.port) and time.time() < timeout:
            pass
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()
        self.join()

    def close(self):
        self._stop_evt.set()


def socket_ping(ip, port, timeout=0.1):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        s.close()
        return True
    except socket.error:
        return False


if __name__ == "__main__":
    with APRSISSimulator() as sim:
        sim.join()