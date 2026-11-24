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
import datetime
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
    FrameHandler,
    MessageHandler,
    ReconnectPolicy,
    aprsis_connector,
    connector_from_url,
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


class MessageAckFuture:
    """
    The pending ack for one sent message.

    ``result()`` blocks until the recipient acks (returning the ack frame),
    rejects (raising MessageRejected) or every retry has timed out (raising
    MaximumRetryExceeded); waiting keeps the client's loop running.
    """

    def __init__(self, future: asyncio.Future, message: Message, runner: _Runner) -> None:
        self._future = future
        self._runner = runner
        self.message = message

    def done(self) -> bool:
        return self._future.done()

    def _wait(self, timeout: float | None) -> None:
        if not self._future.done():

            async def _wait() -> None:
                await asyncio.wait([self._future])

            self._runner.run(_wait(), timeout)

    def result(self, timeout: float | None = None) -> APRSFrame:
        self._wait(timeout)
        return self._future.result()

    def exception(self, timeout: float | None = None) -> BaseException | None:
        self._wait(timeout)
        return self._future.exception()

    def cancel(self) -> bool:
        return self._runner.call(self._future.cancel)


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
        protocol = None
        if self.sync_frame_io is not None:
            self._runner = _Runner(self.sync_frame_io.loop, threaded=False)
            protocol = self.sync_frame_io.protocol
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
        self._runner.run(self.aclient.start(protocol))

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
    def connect(
        cls,
        mycall: str,
        url: str,
        *,
        passcode: str | int | None = None,
        filter: str | None = None,
        kiss_settings: dict | None = None,
        **kwargs: t.Any,
    ) -> "Client":
        """
        Connect from a URL (see ``AsyncClient.connect``); the client runs on
        a background thread::

            with Client.connect("N0CALL-1", "aprsis://rotate.aprs2.net?passcode=12345") as c:
                ...
        """
        connector = connector_from_url(
            url,
            str(mycall),
            passcode=passcode,
            filter=filter,
            kiss_settings=kiss_settings,
        )
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
            self._runner.run(self.aclient.close())
        finally:
            self._runner.stop()

    def __enter__(self) -> "Client":
        return self

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

    def poll(self, seconds: float = 0.0) -> None:
        """Let the client's loop run for ``seconds`` (only needed with ``sync_frame_io``)."""
        self._runner.run(asyncio.sleep(seconds))

    # -- receiving --

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

    def add_frame_handler(self, handler: FrameHandler) -> FrameHandler:
        """
        Call ``handler(frame)`` for every frame as it arrives, like
        ``AsyncClient.on_frame``; usable as a decorator. With a background
        thread (``Client.aprsis`` and friends), the handler runs on it.
        """
        return self._runner.call(self.aclient.on_frame, handler)

    def add_message_handler(self, handler: MessageHandler) -> MessageHandler:
        """
        Call ``handler(message, frame)`` once for each message to ``mycall``
        as it arrives, like ``AsyncClient.on_message``; usable as a decorator.
        """
        return self._runner.call(self.aclient.on_message, handler)

    def on_transmit(self, handler: FrameHandler) -> FrameHandler:
        """Call ``handler(frame)`` for every frame this client sends; usable as a decorator."""
        return self._runner.call(self.aclient.on_transmit, handler)

    def on_frame(self, frame: APRSFrame) -> None:
        """Called by ``read()`` for every frame; calls ``on_message`` for ours."""
        info = frame.info
        if (
            isinstance(info, Message)
            and info.addressee.decode("latin1").strip().upper()
            == str(self.mycall).upper()
        ):
            self.on_message(info, frame)

    def on_message(self, message: Message, frame: APRSFrame) -> None:
        """Called by ``read()`` for each message to ``mycall`` (acks included)."""

    # -- sending --

    def prepare_frame(
        self, info: InformationField, path: list[Address] | str | None = None
    ) -> APRSFrame:
        return self.aclient.prepare_frame(info, path)

    def write(self, frame: APRSFrame) -> None:
        self._runner.call(self.aclient.write, frame)

    def send_message(
        self,
        recipient: str | Address,
        body: str,
        ack: bool = False,
        overflow: OverflowDisposition = OverflowDisposition.Truncate,
        path: list[Address] | str | None = None,
    ) -> MessageAckFuture | list[MessageAckFuture | None] | None:
        """
        Send ``body`` to ``recipient`` without waiting for the ack.

        Text over 67 bytes is cut (``Truncate``) or split on word boundaries
        (``Continue``); a split message returns one result per part. With
        ``ack``, each result is a MessageAckFuture.
        """
        submitted = self._runner.call(
            self.aclient._submit_parts,  # pylint: disable=protected-access
            str(recipient),
            body,
            ack,
            overflow,
            path,
        )
        results: list[MessageAckFuture | None] = [
            MessageAckFuture(future, message, self._runner) if ack else None
            for future, message in submitted
        ]
        if len(results) == 1:
            return results[0]
        return results

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

    def send_status(
        self, status: str, path: list[Address] | str | None = None
    ) -> APRSFrame:
        return self._runner.call(self.aclient.send_status, status, path)

    def send_position(
        self,
        position: Position,
        path: list[Address] | str | None = None,
        *,
        comment: str = "",
        timestamp: datetime.datetime | bool | None = None,
        messaging: bool = True,
    ) -> APRSFrame:
        """Send a position report (see ``AsyncClient.send_position``)."""
        return self._runner.call(
            self.aclient.send_position,
            position,
            path,
            comment=comment,
            timestamp=timestamp,
            messaging=messaging,
        )

    def send(
        self, info: InformationField, path: list[Address] | str | None = None
    ) -> APRSFrame:
        """Frame ``info`` from ``mycall`` and transmit it now; returns the frame."""
        return self._runner.call(self.aclient.send, info, path)

    def send_object(
        self,
        name: str,
        position: Position,
        comment: str = "",
        killed: bool = False,
        timestamp: datetime.datetime | None = None,
        path: list[Address] | str | None = None,
    ) -> APRSFrame:
        """Send an object report (see ``AsyncClient.send_object``)."""
        return self._runner.call(
            self.aclient.send_object, name, position, comment, killed, timestamp, path
        )

    def send_item(
        self,
        name: str,
        position: Position,
        comment: str = "",
        killed: bool = False,
        path: list[Address] | str | None = None,
    ) -> APRSFrame:
        """Send an item report (see ``AsyncClient.send_item``)."""
        return self._runner.call(
            self.aclient.send_item, name, position, comment, killed, path
        )

    def send_bulletin(
        self, text: str, id: str = "0", path: list[Address] | str | None = None
    ) -> APRSFrame:
        """Send a bulletin to ``BLN<id>`` (see ``AsyncClient.send_bulletin``)."""
        return self._runner.call(self.aclient.send_bulletin, text, id, path)

    def schedule(
        self,
        frame: APRSFrame | InformationField,
        delay: float = 0.0,
        at: t.Any = None,
        path: list[Address] | str | None = None,
    ) -> "PacketRecord":
        """Queue ``frame`` in the store for later transmission (see AsyncClient)."""
        return self._runner.run(self.aclient.schedule(frame, delay, at, path))
