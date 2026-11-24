"""
Synchronous APRS client: a thin wrapper over ``AsyncClient``.

Two ways to use it:

- ``Client.aprsis(...)``, ``Client.kiss_tcp(...)`` and ``Client.kiss_serial(...)``
  run the async client on a private event loop in a background thread, so
  acks, retries, reconnects and the send queue run even while the
  application isn't calling ``read()``.
- ``Client(mycall, sync_frame_io)`` with an already started ``aprs.TCP``,
  ``aprs.TCPKISS`` or ``aprs.SerialKISS`` keeps the original API. Its event
  loop is shared with the transport, so it only runs inside the client's
  calls (``read()``, ``send_*``, ``MessageAckFuture.result()``, ``poll()``).
"""

import asyncio
import concurrent.futures
import functools
import threading
import time
import typing as t
import weakref

import attrs
from aprs import APRSFrame, InformationField, Message, Position
from aprs.aprsis import TCP, create_aprsis_connection
from aprs.kiss import TCPKISS, SerialKISS
from ax253 import Address, FrameDecodeProtocol

from .aio import (
    DEFAULT_DUPLICATE_WINDOW,
    AsyncClient,
    Connector,
    ReconnectPolicy,
    aprsis_connector,
    kiss_serial_connector,
    kiss_tcp_connector,
)
from .common import (
    DEFAULT_MESSAGE_RETRY,
    DEFAULT_MESSAGE_RETRY_PERIOD,
    MESSAGE_TEXT_MAX,
    SEQ_NUM_MAX,
    MaximumRetryExceeded,
    MessageRejected,
    OverflowDisposition,
    parse_ack,
    path_split,
    split_message,
    truncate_message,
)

if t.TYPE_CHECKING:
    from .store import PacketRecord, PacketStore

__all__ = [
    "Client",
    "MESSAGE_TEXT_MAX",
    "MaximumRetryExceeded",
    "MessageAckFuture",
    "MessageRejected",
    "OverflowDisposition",
    "SEQ_NUM_MAX",
    "parse_ack",
    "path_split",
    "split_message",
    "truncate_message",
]

_T = t.TypeVar("_T")


_DRIVERS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Driver]" = (
    weakref.WeakKeyDictionary()
)


class _Driver:
    """Which thread is running a shared (inline) loop right now."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: int | None = None


class _Runner:
    """
    Runs coroutines on the client's loop, from the calling thread.

    Threaded: the loop runs forever in a daemon thread and calls are handed
    to it. Inline (``sync_frame_io``): the loop is shared with the transport
    and every legacy client, so the calling thread runs it until the call is
    done; a call from another thread while the loop runs is handed to it.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, threaded: bool) -> None:
        self.loop = loop
        self.thread: threading.Thread | None = None
        if threaded:
            self.thread = threading.Thread(
                target=loop.run_forever, name="base_aprs_client", daemon=True
            )
            self.thread.start()
            return
        self._driver = _DRIVERS.setdefault(loop, _Driver())

    def run(
        self, coro: t.Coroutine[t.Any, t.Any, _T], timeout: float | None = None
    ) -> _T:
        if self.thread is not None:
            return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)
        driver = self._driver
        if driver.thread == threading.get_ident():
            coro.close()
            raise RuntimeError(
                "Client methods can't be called from the event loop; "
                "use the AsyncClient (client.aclient) there"
            )
        deadline = None if timeout is None else time.monotonic() + timeout
        handed: concurrent.futures.Future | None = None
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if driver.lock.acquire(blocking=False):
                driver.thread = threading.get_ident()
                try:
                    # wait_for only times out ``coro`` itself; a handed-over
                    # call is waited for here, so it isn't left unscheduled
                    target = coro if handed is None else asyncio.wrap_future(handed)
                    return self.loop.run_until_complete(
                        asyncio.wait_for(target, remaining)
                    )
                finally:
                    driver.thread = None
                    driver.lock.release()
            # another thread is running the loop: hand the call to it
            if handed is None:
                handed = asyncio.run_coroutine_threadsafe(coro, self.loop)
            try:
                wait = 0.05 if remaining is None else max(0.0, min(0.05, remaining))
                return handed.result(wait)
            except concurrent.futures.TimeoutError:
                if remaining is not None and remaining <= 0:
                    handed.cancel()
                    raise TimeoutError from None

    def call(self, fn: t.Callable[..., _T], *args: t.Any, **kwargs: t.Any) -> _T:
        async def _call() -> _T:
            return fn(*args, **kwargs)

        return self.run(_call())

    def stop(self) -> None:
        if self.thread is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(10)
            self.loop.close()
            self.thread = None


class MessageAckFuture(concurrent.futures.Future):
    """
    The pending ack for one sent message: a ``concurrent.futures.Future``.

    ``result()`` blocks until the recipient acks (returning the ack frame),
    rejects (raising MessageRejected) or every retry has timed out (raising
    MaximumRetryExceeded); waiting keeps the client's loop running. A
    message sent without ``ack`` is already resolved to None.

    As a real ``Future`` it works with ``concurrent.futures.wait()``,
    ``as_completed()`` and ``add_done_callback()``; those don't run the
    client's loop themselves, so with an inline (``sync_frame_io``) client
    call ``poll()`` or ``result()`` to let acks arrive.
    """

    def __init__(self, future: asyncio.Future, message: Message, runner: _Runner) -> None:
        super().__init__()
        self._future = future
        self._runner = runner
        self.message = message
        if future.done():
            self._copy(future)
        else:
            future.add_done_callback(self._copy)

    def _copy(self, future: asyncio.Future) -> None:
        if future.cancelled():
            super().cancel()
            self.set_running_or_notify_cancel()
        elif future.exception() is not None:
            self.set_exception(future.exception())
        else:
            self.set_result(future.result())

    def _wait(self, timeout: float | None) -> None:
        if not self._future.done():

            async def _wait() -> None:
                await asyncio.wait([self._future])

            self._runner.run(_wait(), timeout)

    def result(self, timeout: float | None = None) -> APRSFrame | None:
        self._wait(timeout)
        return super().result(0)

    def exception(self, timeout: float | None = None) -> BaseException | None:
        self._wait(timeout)
        return super().exception(0)

    def cancel(self) -> bool:
        """Stop resending the message; True if it was still waiting for an ack."""
        if self._future.done():
            return False
        return self._runner.call(self._future.cancel)


_F = t.TypeVar("_F", bound=t.Callable[..., t.Any])


def _autostart(method: _F) -> _F:
    """Start the client (connect) before the first call that needs it."""

    @functools.wraps(method)
    def wrapper(self: "Client", *args: t.Any, **kwargs: t.Any) -> t.Any:
        self._ensure_started()  # pylint: disable=protected-access
        return method(self, *args, **kwargs)

    return t.cast(_F, wrapper)


def _connector_for(sync_frame_io: t.Any) -> Connector | None:
    """Build a reconnecting connector from an ``aprs`` sync transport's settings."""
    if isinstance(sync_frame_io, TCP):
        args, kwargs = sync_frame_io.args, sync_frame_io.kwargs

        async def connect() -> FrameDecodeProtocol:
            _, protocol = await create_aprsis_connection(*args, **kwargs)
            return protocol

        return connect
    if isinstance(sync_frame_io, TCPKISS):
        return kiss_tcp_connector(*sync_frame_io.address)
    if isinstance(sync_frame_io, SerialKISS):
        return kiss_serial_connector(sync_frame_io.port, int(sync_frame_io.speed))
    return None


@attrs.define(eq=False)
class Client:
    """
    Sends messages with ack and retry, acks incoming messages addressed to
    ``mycall``, and sends status and position reports.

    Subclasses may override ``on_frame(frame)`` and ``on_message(message,
    frame)``; they are called from ``read()`` for each frame it returns.
    """

    mycall: Address = attrs.field(converter=Address.from_any)
    sync_frame_io: t.Any = attrs.field(default=None)
    default_path: list[Address] = attrs.field(factory=list, converter=path_split)
    destination: Address = attrs.field(
        default=Address.from_any("APZ069"), converter=Address.from_any
    )
    _initial_sequence_number: int | None = attrs.field(
        default=None, alias="next_message_sequence_number"
    )
    store: "PacketStore | None" = attrs.field(default=None, kw_only=True)
    reconnect: ReconnectPolicy | None = attrs.field(factory=ReconnectPolicy, kw_only=True)
    retries: int = attrs.field(default=DEFAULT_MESSAGE_RETRY, kw_only=True)
    retry_period: float = attrs.field(default=DEFAULT_MESSAGE_RETRY_PERIOD, kw_only=True)
    duplicate_window: float = attrs.field(default=DEFAULT_DUPLICATE_WINDOW, kw_only=True)
    reply_ack: bool = attrs.field(default=True, kw_only=True)
    connector: Connector | None = attrs.field(default=None, kw_only=True)

    aclient: AsyncClient = attrs.field(init=False)
    _runner: _Runner = attrs.field(init=False)
    _started: bool = attrs.field(default=False, init=False)

    def __attrs_post_init__(self) -> None:
        kwargs: dict[str, t.Any] = dict(
            mycall=self.mycall,
            default_path=self.default_path,
            destination=self.destination,
            store=self.store,
            reconnect=self.reconnect,
            retries=self.retries,
            retry_period=self.retry_period,
            duplicate_window=self.duplicate_window,
            reply_ack=self.reply_ack,
        )
        if self._initial_sequence_number is not None:
            kwargs["next_message_sequence_number"] = self._initial_sequence_number
        if self.sync_frame_io is not None:
            self._runner = _Runner(self.sync_frame_io.loop, threaded=False)
            kwargs["connector"] = self.connector or _connector_for(self.sync_frame_io)
        else:
            if self.connector is None:
                raise ValueError("Client needs a sync_frame_io or a connector")
            self._runner = _Runner(asyncio.new_event_loop(), threaded=True)
            kwargs["connector"] = self.connector
        self.aclient = AsyncClient(**kwargs)
        if self.sync_frame_io is not None:
            # keep the transport's stop() pointed at the live connection
            self.aclient.connect_handlers.append(self._adopt_protocol)

    def start(self) -> "Client":
        """
        Connect and start acking, retrying and reconnecting.

        ``with client:`` calls this. Sending or reading before ``start()``
        starts the client then, so a script can skip it; constructing a
        ``Client`` never touches the network.
        """
        if not self._started:
            self._started = True
            protocol = None if self.sync_frame_io is None else self.sync_frame_io.protocol
            self._runner.run(self.aclient.start(protocol))
        return self

    def _ensure_started(self) -> None:
        if not self._started:
            self.start()

    def _adopt_protocol(self, protocol: FrameDecodeProtocol) -> None:
        self.sync_frame_io._protocol = protocol  # pylint: disable=protected-access

    # -- constructors --

    @classmethod
    def aprsis(
        cls,
        mycall: str,
        host: str = "rotate.aprs.net",
        port: int = 14580,
        passcode: str = "-1",
        filter: str | None = None,
        **kwargs: t.Any,
    ) -> "Client":
        """Connect to APRS-IS; the client runs on a background thread."""
        connector = aprsis_connector(str(mycall), host, port, passcode, filter)
        return cls(mycall=mycall, connector=connector, **kwargs)

    @classmethod
    def kiss_tcp(
        cls,
        mycall: str,
        host: str,
        port: int,
        kiss_settings: dict | None = None,
        **kwargs,
    ) -> "Client":
        """Connect to a KISS-over-TCP TNC; the client runs on a background thread."""
        return cls(
            mycall=mycall,
            connector=kiss_tcp_connector(host, port, kiss_settings),
            **kwargs,
        )

    @classmethod
    def kiss_serial(
        cls,
        mycall: str,
        port: str,
        baudrate: int = 9600,
        kiss_settings: dict | None = None,
        **kwargs: t.Any,
    ) -> "Client":
        """Open a serial KISS TNC; the client runs on a background thread."""
        return cls(
            mycall=mycall,
            connector=kiss_serial_connector(port, baudrate, kiss_settings),
            **kwargs,
        )

    # -- lifecycle --

    def close(self) -> None:
        """Close the connection and stop the client."""
        try:
            if self._started:
                self._runner.run(self.aclient.close())
        finally:
            self._runner.stop()

    def __enter__(self) -> "Client":
        return self.start()

    def __exit__(self, *exc_info: t.Any) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self.aclient.connected

    @property
    def next_message_sequence_number(self) -> int:
        """The number the next acked message will carry."""
        return self.aclient.next_message_sequence_number

    @property
    def outstanding_messages(self) -> dict:
        """Messages waiting for an ack, by message number."""
        return self.aclient.outstanding

    @_autostart
    def poll(self, seconds: float = 0.0) -> None:
        """Let the client's loop run for ``seconds`` (only needed with ``sync_frame_io``)."""
        self._runner.run(asyncio.sleep(seconds))

    # -- receiving --

    @_autostart
    def read(self, min_frames: int | None = -1) -> list[APRSFrame]:
        """
        Frames received since the last read, calling ``on_frame`` for each.

        ``min_frames=-1`` returns what has arrived without waiting; a number
        waits for at least that many; None reads until the client closes.
        Duplicate messages are left out.
        """

        async def _read() -> list[APRSFrame]:
            await asyncio.sleep(0)  # let pending input be decoded
            frames = self.aclient.recv_nowait()
            while min_frames is None or len(frames) < min_frames:
                try:
                    frames.append(await self.aclient.recv())
                except EOFError:
                    break
                frames.extend(self.aclient.recv_nowait())
            return frames

        frames = self._runner.run(_read())
        for frame in frames:
            self.on_frame(frame)
        return frames

    def on_frame(self, frame: APRSFrame) -> None:
        """Called by ``read()`` for every frame; calls ``on_message`` for ours."""
        info = frame.info
        if (
            isinstance(info, Message)
            and info.addressee.decode("latin1").strip().upper()
            == str(self.mycall).upper()
            and not (info.number is None and parse_ack(info.text))
        ):
            self.on_message(info, frame)

    def on_message(self, message: Message, frame: APRSFrame) -> None:
        """Called by ``read()`` for each message to ``mycall``, without acks or rejects."""

    # -- sending --

    def prepare_frame(
        self, info: InformationField, path: list[Address] | str | None = None
    ) -> APRSFrame:
        return self.aclient.prepare_frame(info, path)

    @_autostart
    def write(self, frame: APRSFrame) -> None:
        self._runner.call(self.aclient.write, frame)

    @_autostart
    def send_message(
        self,
        recipient: str | Address,
        body: str,
        ack: bool = False,
        overflow: OverflowDisposition = OverflowDisposition.Truncate,
        path: list[Address] | str | None = None,
    ) -> list[MessageAckFuture]:
        """
        Send ``body`` to ``recipient`` without waiting for the ack.

        Returns one ``MessageAckFuture`` per part: text over 67 bytes is cut
        (``Truncate``, one part) or split on word boundaries (``Continue``).
        With ``ack``, each future resolves to the ack frame; without, they
        are already resolved to None. The same shape as
        ``AsyncClient.submit_message``::

            for future in client.send_message("N0CALL-2", text, ack=True):
                future.result(timeout=60)
        """
        submitted = self._runner.call(
            self.aclient._submit_parts,  # pylint: disable=protected-access
            str(recipient),
            body,
            ack,
            overflow,
            path,
        )
        return [
            MessageAckFuture(future, message, self._runner)
            for future, message in submitted
        ]

    @_autostart
    def submit_message(
        self,
        recipient: str | Address,
        body: str,
        overflow: OverflowDisposition = OverflowDisposition.Truncate,
        path: list[Address] | str | None = None,
    ) -> list[MessageAckFuture]:
        """
        Send ``body`` with acks, without waiting: one future per part.

        Like ``send_message(..., ack=True)``, but always a list, so the
        result's type doesn't depend on whether the text was split.
        """
        submitted = self._runner.call(
            self.aclient._submit_parts,  # pylint: disable=protected-access
            str(recipient),
            body,
            True,
            overflow,
            path,
        )
        return [
            MessageAckFuture(future, message, self._runner)
            for future, message in submitted
        ]

    @_autostart
    def send_status(
        self, status: str, path: list[Address] | str | None = None
    ) -> APRSFrame:
        return self._runner.call(self.aclient.send_status, status, path)

    @_autostart
    def send_position(
        self, position: Position, path: list[Address] | str | None = None
    ) -> APRSFrame:
        return self._runner.call(self.aclient.send_position, position, path)

    @_autostart
    def schedule(
        self,
        frame: APRSFrame | InformationField,
        delay: float = 0.0,
        at: t.Any = None,
        path: list[Address] | str | None = None,
    ) -> "PacketRecord":
        """Queue ``frame`` in the store for later transmission (see AsyncClient)."""
        return self._runner.run(self.aclient.schedule(frame, delay, at, path))
