"""Miniature APRS-IS-like relay server for tests and local experiments."""

import asyncio
import logging
import threading
import time

import attrs

logger = logging.getLogger(__name__)


SERVER_BRINGUP_TIMEOUT = 10
SERVER_BANNER = b"# base-aprs-client APRSISSimulator\r\n"


@attrs.define(eq=False)
class APRSISSimulator:
    """
    Miniature APRS-IS-like server for testing.

    Every TNC2 line received from one client is relayed verbatim to every
    other connected client. Login lines (``user ...``) are acknowledged with
    a ``# logresp`` comment and are never relayed. Lines beginning with ``#``
    are treated as client comments and dropped.

    The server runs its own event loop in a background thread so it can be
    used from synchronous test code::

        from aprs import TCP
        from base_aprs_client import APRSISSimulator, Client

        with APRSISSimulator() as sim:
            with TCP(host=sim.host, port=sim.port) as t1, \\
                    TCP(host=sim.host, port=sim.port) as t2:
                client1 = Client("N0CALL-1", t1)
                client2 = Client("N0CALL-2", t2)
                client1.send_status("Foobar")
                print(client2.read(min_frames=1))

    Pass ``port=0`` (the default) to bind an ephemeral port; the bound port
    is available as ``.port`` once the server is started.
    """

    host: str = attrs.field(default="127.0.0.1")
    port: int = attrs.field(default=0)
    received: list[tuple[tuple[str, int], bytes]] = attrs.field(factory=list)
    """Every relayed line, with the peer address that sent it."""

    _writers: list[asyncio.StreamWriter] = attrs.field(factory=list, init=False)
    _loop: asyncio.AbstractEventLoop | None = attrs.field(default=None, init=False)
    _server: asyncio.AbstractServer | None = attrs.field(default=None, init=False)
    _thread: threading.Thread | None = attrs.field(default=None, init=False)
    _started: threading.Event = attrs.field(factory=threading.Event, init=False)
    _stopped: asyncio.Event | None = attrs.field(default=None, init=False)
    _error: BaseException | None = attrs.field(default=None, init=False)

    def _broadcast(self, sender: asyncio.StreamWriter | None, line: bytes) -> None:
        for writer in list(self._writers):
            if writer is sender or writer.is_closing():
                continue
            writer.write(line)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        addr = writer.get_extra_info("peername")
        writer.write(SERVER_BANNER)
        while True:
            line = await reader.readline()
            if not line:  # EOF
                break
            # APRS-IS only strips the line terminator; trailing spaces are
            # significant (e.g. in message text).
            stripped = line.rstrip(b"\r\n")
            if not stripped.strip():
                continue
            if stripped.startswith(b"user "):
                username = stripped.split()[1].decode("latin1", "replace")
                logger.info("%r logs in as %s", addr, username)
                writer.write(
                    b"# logresp %s verified, server SIMULATOR\r\n"
                    % username.encode("latin1", "replace")
                )
                continue
            if stripped.startswith(b"#"):
                continue
            # normalize line endings to CRLF, as APRS-IS does
            out = stripped + b"\r\n"
            self.received.append((addr, out))
            self._broadcast(writer, out)
            await writer.drain()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._stopped is not None and self._stopped.is_set():
            # accepted just as the server stopped: don't keep it open
            writer.close()
            return
        self._writers.append(writer)
        try:
            await self._handle_client(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:  # pylint: disable=broad-except
            logger.exception("Exception handling connection")
        finally:
            self._writers.remove(writer)
            writer.close()

    async def _run(self) -> None:
        self._stopped = asyncio.Event()
        self._server = await asyncio.start_server(
            self._handle_connection,
            self.host,
            self.port,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("Serving on %s:%s", self.host, self.port)
        self._started.set()
        async with self._server:
            await self._stopped.wait()
            self._server.close()  # stop accepting before closing connections
            for writer in list(self._writers):
                writer.close()
            # let every connection handler finish, including any accepted
            # just now that hasn't started: Python 3.11 doesn't wait for them
            # and would leave those sockets open when the loop closes
            handlers = asyncio.all_tasks() - {asyncio.current_task()}
            await asyncio.gather(*handlers, return_exceptions=True)
            await asyncio.sleep(0)  # transports finish closing

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._run())
        except BaseException as exc:  # pylint: disable=broad-except
            self._error = exc
            self._started.set()
        finally:
            self._loop.close()

    def start(self) -> "APRSISSimulator":
        """Start the server thread and block until it is accepting connections."""
        self._thread = threading.Thread(
            target=self._thread_main, name="APRSISSimulator", daemon=True
        )
        self._thread.start()
        if not self._started.wait(SERVER_BRINGUP_TIMEOUT):
            raise TimeoutError("APRSISSimulator did not start")
        if self._error is not None:
            raise self._error
        return self

    @property
    def n_clients(self) -> int:
        """Number of currently connected clients."""
        return len(self._writers)

    def wait_for_clients(self, n: int, timeout: float = SERVER_BRINGUP_TIMEOUT) -> None:
        """Block until at least ``n`` clients are connected."""
        deadline = time.monotonic() + timeout
        while self.n_clients < n:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Only {self.n_clients} of {n} clients connected")
            time.sleep(0.01)

    def close(self) -> None:
        """Stop the server and wait for its thread to exit."""
        if self._loop is not None and self._stopped is not None:
            try:
                self._loop.call_soon_threadsafe(self._stopped.set)
            except RuntimeError:
                pass  # loop already closed
        if self._thread is not None:
            self._thread.join(SERVER_BRINGUP_TIMEOUT)

    def __enter__(self) -> "APRSISSimulator":
        return self.start()

    def __exit__(self, exc_type, exc_value, exc_tb) -> None:
        self.close()


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    with APRSISSimulator(port=14588) as sim:
        sim._thread.join()  # pylint: disable=protected-access
