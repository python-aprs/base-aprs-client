"""
Async-first APRS client.

``AsyncClient`` runs on the ``aprs`` async protocols (APRS-IS or KISS over
TCP or serial). It acks incoming messages, retries outgoing ones on a timer
until they are acked, rejected or time out, suppresses duplicate messages,
supports reply-acks, reconnects when the transport drops, and optionally
logs every frame to a ``PacketStore`` and transmits its delayed send queue::

    async with AsyncClient.aprsis("N0CALL-1", filter="r/49/-72/50") as client:
        ack = await client.send_message("N0CALL-2", "hello", ack=True)
        async for frame in client:
            print(frame)
"""

import asyncio
import contextlib
import inspect
import logging
import random
import typing as t
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import attrs
from aprs import (
    APRSFrame,
    DataType,
    InformationField,
    Message,
    Position,
    PositionReport,
    StatusReport,
)
from aprs.aprsis import create_aprsis_connection
from aprs.constants import APRSIS_FILTER_PORT, APRSIS_SERVERS
from aprs.kiss import create_serial_connection, create_tcp_connection
from ax253 import Address, FrameDecodeProtocol

from .common import (
    DEFAULT_MESSAGE_RETRY,
    DEFAULT_MESSAGE_RETRY_PERIOD,
    SEQ_NUM_MAX,
    MaximumRetryExceeded,
    MessageRejected,
    OverflowDisposition,
    parse_ack,
    parse_number,
    path_split,
    split_message,
    truncate_message,
)

if t.TYPE_CHECKING:  # the db extra is optional
    from .store import PacketRecord, PacketStore

logger = logging.getLogger(__name__)

Connector = Callable[[], Awaitable[FrameDecodeProtocol]]
"""Opens a new connection (logging in or applying settings) and returns its protocol."""
FrameHandler = Callable[[APRSFrame], t.Any]
MessageHandler = Callable[[Message, APRSFrame], t.Any]

DEFAULT_DUPLICATE_WINDOW = 30.0
DEFAULT_MAX_QUEUED_FRAMES = 10_000
_CLOSED = object()


class NotConnected(ConnectionError):
    """The client has no open connection to write to."""


# --- connectors --------------------------------------------------------------


def aprsis_connector(
    user: str,
    host: str = APRSIS_SERVERS[0],
    port: int = APRSIS_FILTER_PORT,
    passcode: str = "-1",
    filter: str | None = None,
) -> Connector:
    """Connect and log in to APRS-IS, sending ``filter`` with every login."""

    async def connect() -> FrameDecodeProtocol:
        _, protocol = await create_aprsis_connection(
            host=host,
            port=port,
            user=user,
            passcode=passcode,
            command=f"filter {filter}" if filter else "",
        )
        return protocol

    return connect


def kiss_tcp_connector(
    host: str, port: int, kiss_settings: dict | None = None
) -> Connector:
    """Connect to a KISS-over-TCP TNC (e.g. Dire Wolf), applying ``kiss_settings``."""

    async def connect() -> FrameDecodeProtocol:
        _, protocol = await create_tcp_connection(
            host=host, port=port, kiss_settings=kiss_settings
        )
        await protocol.connection_future
        return protocol

    return connect


def kiss_serial_connector(
    port: str, baudrate: int = 9600, kiss_settings: dict | None = None
) -> Connector:
    """Open a serial KISS TNC, applying ``kiss_settings``."""

    async def connect() -> FrameDecodeProtocol:
        _, protocol = await create_serial_connection(
            port=port, baudrate=baudrate, kiss_settings=kiss_settings
        )
        await protocol.connection_future
        return protocol

    return connect


@attrs.define
class ReconnectPolicy:
    """Exponential backoff with jitter between reconnect attempts."""

    initial_delay: float = 1.0
    max_delay: float = 60.0
    factor: float = 2.0
    jitter: float = 0.1
    """Each delay is randomly varied by up to this fraction, up or down."""
    max_attempts: int | None = None
    """Give up after this many failed attempts in a row (None: never)."""

    def delays(self) -> Iterator[float]:
        delay = self.initial_delay
        attempt = 0
        while self.max_attempts is None or attempt < self.max_attempts:
            attempt += 1
            yield max(0.0, delay * (1 + random.uniform(-self.jitter, self.jitter)))
            delay = min(self.max_delay, delay * self.factor)


# --- outstanding messages ----------------------------------------------------


@attrs.define(eq=False)
class OutstandingMessage:
    """A message waiting for an ack, retried on a timer."""

    message: Message
    path: list[Address]
    future: asyncio.Future
    remaining_tries: int
    period: float
    timer: asyncio.TimerHandle | None = None

    @property
    def recipient(self) -> str:
        return self.message.addressee.decode("latin1")


# --- the client --------------------------------------------------------------


def _same_station(a: str, b: str) -> bool:
    return a.strip().upper() == b.strip().upper()


@attrs.define(eq=False)
class AsyncClient:
    """
    An APRS station on one connection.

    Pass a ``connector`` (see ``aprsis_connector``, ``kiss_tcp_connector``,
    ``kiss_serial_connector``) or use the ``aprsis``/``kiss_tcp``/
    ``kiss_serial`` constructors. ``start()`` (or ``async with``) connects.
    """

    mycall: Address = attrs.field(converter=Address.from_any)
    connector: Connector | None = attrs.field(default=None)
    default_path: list[Address] = attrs.field(factory=list, converter=path_split)
    destination: Address = attrs.field(default="APZ069", converter=Address.from_any)
    store: "PacketStore | None" = attrs.field(default=None)
    """Log every frame heard and sent, and transmit its queue (``schedule``)."""
    reconnect: ReconnectPolicy | None = attrs.field(factory=ReconnectPolicy)
    """How to reconnect when the connection drops; None to stop instead."""
    retries: int = attrs.field(default=DEFAULT_MESSAGE_RETRY)
    retry_period: float = attrs.field(default=DEFAULT_MESSAGE_RETRY_PERIOD)
    duplicate_window: float = attrs.field(default=DEFAULT_DUPLICATE_WINDOW)
    """Seconds to remember incoming messages, so each is delivered once (0: off)."""
    reply_ack: bool = attrs.field(default=True)
    """Number outgoing messages "{MM}" + reply-ack, advertising reply-ack support."""
    next_message_sequence_number: int = attrs.field(
        factory=lambda: random.randint(0, SEQ_NUM_MAX)
    )
    max_queued_frames: int = attrs.field(default=DEFAULT_MAX_QUEUED_FRAMES)
    """Frames kept for ``recv``/``async for`` before the oldest are dropped."""

    protocol: FrameDecodeProtocol | None = attrs.field(default=None, init=False)
    outstanding: dict[bytes, OutstandingMessage] = attrs.field(factory=dict, init=False)
    frame_handlers: list[FrameHandler] = attrs.field(factory=list, init=False)
    message_handlers: list[MessageHandler] = attrs.field(factory=list, init=False)
    connect_handlers: list[Callable[[FrameDecodeProtocol], t.Any]] = attrs.field(
        factory=list, init=False
    )
    dropped_frames: int = attrs.field(default=0, init=False)

    _frames: asyncio.Queue = attrs.field(init=False, default=None)
    _connected: asyncio.Event = attrs.field(init=False, default=None)
    _wakeup: asyncio.Event = attrs.field(init=False, default=None)
    _tasks: set[asyncio.Task] = attrs.field(factory=set, init=False)
    _seen: dict[tuple, float] = attrs.field(factory=dict, init=False)
    _reply_ack_stations: set[str] = attrs.field(factory=set, init=False)
    _pending_reply_acks: dict[str, bytes] = attrs.field(factory=dict, init=False)
    _started: bool = attrs.field(default=False, init=False)
    _closing: bool = attrs.field(default=False, init=False)

    # -- constructors --

    @classmethod
    def aprsis(
        cls,
        mycall: str,
        host: str = APRSIS_SERVERS[0],
        port: int = APRSIS_FILTER_PORT,
        passcode: str = "-1",
        filter: str | None = None,
        **kwargs: t.Any,
    ) -> "AsyncClient":
        connector = aprsis_connector(str(mycall), host, port, passcode, filter)
        return cls(mycall=mycall, connector=connector, **kwargs)

    @classmethod
    def kiss_tcp(
        cls,
        mycall: str,
        host: str,
        port: int,
        kiss_settings: dict | None = None,
        **kwargs: t.Any,
    ) -> "AsyncClient":
        connector = kiss_tcp_connector(host, port, kiss_settings)
        return cls(mycall=mycall, connector=connector, **kwargs)

    @classmethod
    def kiss_serial(
        cls,
        mycall: str,
        port: str,
        baudrate: int = 9600,
        kiss_settings: dict | None = None,
        **kwargs: t.Any,
    ) -> "AsyncClient":
        connector = kiss_serial_connector(port, baudrate, kiss_settings)
        return cls(mycall=mycall, connector=connector, **kwargs)

    # -- lifecycle --

    async def start(self, protocol: FrameDecodeProtocol | None = None) -> "AsyncClient":
        """
        Connect (or use an already connected ``protocol``) and start the
        receive loop, and the send-queue loop when there is a ``store``.
        """
        if self._started:
            return self
        self._frames = asyncio.Queue()
        self._connected = asyncio.Event()
        self._wakeup = asyncio.Event()
        if protocol is None:
            if self.connector is None:
                raise ValueError("AsyncClient needs a connector or a protocol")
            protocol = await self.connector()
        self._started = True
        self._set_protocol(protocol)
        self._spawn(self._run_reader())
        if self.store is not None:
            await self.store.create_all()
            self._spawn(self._run_sender())
        return self

    async def close(self) -> None:
        """Stop the loops, cancel outstanding messages and close the connection."""
        if self._closing:
            return
        self._closing = True
        for om in list(self.outstanding.values()):
            self._finish(om)
            om.future.cancel()
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.protocol is not None and self.protocol.transport is not None:
            self.protocol.transport.close()
            await asyncio.sleep(0)  # let the transport release the socket/port
        if self._frames is not None:
            self._frames.put_nowait(_CLOSED)

    async def __aenter__(self) -> "AsyncClient":
        return await self.start()

    async def __aexit__(self, *exc_info: t.Any) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        return self._connected is not None and self._connected.is_set()

    async def wait_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    def _spawn(self, coro: t.Coroutine) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Background task failed", exc_info=task.exception())

    def _set_protocol(self, protocol: FrameDecodeProtocol) -> None:
        self.protocol = protocol
        self._connected.set()
        self._wakeup.set()
        for handler in self.connect_handlers:
            self._call_handler(handler, protocol)

    # -- receiving --

    async def _run_reader(self) -> None:
        try:
            while True:
                protocol = self.protocol
                assert protocol is not None
                async for frame in protocol.read():
                    await self._handle_frame(frame)
                self._connected.clear()
                if self._closing:
                    return
                logger.warning("%s: connection lost", self.mycall)
                if not await self._reconnect():
                    return
        finally:
            self._connected.clear()
            if self._frames is not None:
                self._frames.put_nowait(_CLOSED)

    async def _reconnect(self) -> bool:
        if self.reconnect is None or self.connector is None:
            return False
        for delay in self.reconnect.delays():
            await asyncio.sleep(delay)
            if self._closing:
                return False
            try:
                protocol = await self.connector()
            except OSError as exc:
                logger.warning("%s: reconnect failed: %s", self.mycall, exc)
                continue
            logger.info("%s: reconnected", self.mycall)
            self._set_protocol(protocol)
            return True
        logger.error("%s: giving up reconnecting", self.mycall)
        return False

    async def _handle_frame(self, frame: APRSFrame) -> None:
        if not isinstance(frame, APRSFrame):
            return
        if self.store is not None:
            try:
                await self.store.record_received(frame)
            except Exception:  # pylint: disable=broad-except
                logger.exception("Could not log received frame %s", frame)
        info = frame.info
        to_me = isinstance(info, Message) and _same_station(
            info.addressee.decode("latin1"), str(self.mycall)
        )
        if to_me and not self._handle_message(t.cast(Message, info), frame):
            return  # duplicate
        self._deliver(frame, to_me)

    def _handle_message(self, message: Message, frame: APRSFrame) -> bool:
        """Ack, resolve outstanding messages; False if ``message`` is a duplicate."""
        source = str(frame.source)
        if message.number:
            number, reply_ack_capable, acked = parse_number(message.number)
            if reply_ack_capable:
                self._reply_ack_stations.add(source.upper())
                self._pending_reply_acks[source.upper()] = number
            if acked:
                self._resolve(acked, frame, rejected=False)
            self._send_ack(frame.source, number, reply_ack_capable)
            key: tuple = (source.upper(), number)
        else:
            ack = parse_ack(message.text)
            if ack is not None:
                kind, number = ack
                self._resolve(number, frame, rejected=kind == b"rej")
            key = (source.upper(), message.text)
        return not self._is_duplicate(key)

    def _is_duplicate(self, key: tuple) -> bool:
        if self.duplicate_window <= 0:
            return False
        now = asyncio.get_running_loop().time()
        horizon = now - self.duplicate_window
        for old in [k for k, seen in self._seen.items() if seen < horizon]:
            del self._seen[old]
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    def _deliver(self, frame: APRSFrame, to_me: bool) -> None:
        if self.max_queued_frames and self._frames.qsize() >= self.max_queued_frames:
            self._frames.get_nowait()
            self.dropped_frames += 1
        self._frames.put_nowait(frame)
        for handler in self.frame_handlers:
            self._call_handler(handler, frame)
        if to_me:
            for handler in self.message_handlers:
                self._call_handler(handler, frame.info, frame)

    def _call_handler(self, handler: Callable, *args: t.Any) -> None:
        try:
            result = handler(*args)
            if inspect.isawaitable(result):
                self._spawn(t.cast(t.Coroutine, result))
        except Exception:  # pylint: disable=broad-except
            logger.exception("Handler %r failed", handler)

    def on_frame(self, handler: FrameHandler) -> FrameHandler:
        """Call ``handler(frame)`` (or await it) for every frame delivered."""
        self.frame_handlers.append(handler)
        return handler

    def on_message(self, handler: MessageHandler) -> MessageHandler:
        """Call ``handler(message, frame)`` for each message to ``mycall``, once."""
        self.message_handlers.append(handler)
        return handler

    async def recv(self, timeout: float | None = None) -> APRSFrame:
        """The next frame; raises EOFError once the client is closed."""
        frame = await asyncio.wait_for(self._frames.get(), timeout)
        if frame is _CLOSED:
            self._frames.put_nowait(_CLOSED)  # wake other readers too
            raise EOFError("client closed")
        return frame

    def recv_nowait(self) -> list[APRSFrame]:
        """Every frame received so far and not yet returned."""
        frames = []
        while not self._frames.empty():
            frame = self._frames.get_nowait()
            if frame is _CLOSED:
                self._frames.put_nowait(_CLOSED)
                break
            frames.append(frame)
        return frames

    async def __aiter__(self) -> AsyncIterator[APRSFrame]:
        while True:
            try:
                yield await self.recv()
            except EOFError:
                return

    # -- sending --

    def prepare_frame(
        self, info: InformationField, path: list[Address] | str | None = None
    ) -> APRSFrame:
        # ui() sets the AX.25 end-of-address bit, which KISS TNCs need
        return APRSFrame.ui(
            destination=self.destination,
            source=self.mycall,
            path=path_split(path) if path else self.default_path,
            info=info,  # ty: ignore[invalid-argument-type]  # ui() is typed for bytes
        )

    def write(self, frame: APRSFrame, record: bool = True) -> None:
        """Transmit ``frame`` now; raises NotConnected if the connection is down."""
        if not self.connected or self.protocol is None:
            raise NotConnected(f"{self.mycall} is not connected")
        self.protocol.write(frame)
        if record and self.store is not None:
            self._spawn(self._record_sent(frame))

    async def _record_sent(self, frame: APRSFrame) -> None:
        assert self.store is not None
        try:
            await self.store.record_sent(frame)
        except Exception:  # pylint: disable=broad-except
            logger.exception("Could not log sent frame %s", frame)

    def _burn_message_sequence_number(self) -> bytes:
        number = b"%x" % self.next_message_sequence_number
        self.next_message_sequence_number = (
            self.next_message_sequence_number + 1
        ) % SEQ_NUM_MAX
        return number

    def _message(self, recipient: str, text: str, number: bytes | None) -> Message:
        return Message(
            raw=b"",
            data_type=DataType.MESSAGE,
            data=b"",
            addressee=str(recipient).encode("latin1"),
            text=text.encode(),
            number=number,
        )

    def _send_ack(self, recipient: Address, number: bytes, reply_ack: bool) -> None:
        text = "ack" + number.decode("latin1") + ("}" if reply_ack else "")
        try:
            self.write(self.prepare_frame(self._message(str(recipient), text, None)))
        except NotConnected:
            logger.warning("Could not ack %s from %s: not connected", number, recipient)

    def submit_message(
        self,
        recipient: str,
        body: str,
        ack: bool = False,
        overflow: OverflowDisposition = OverflowDisposition.Truncate,
        path: list[Address] | str | None = None,
    ) -> list[asyncio.Future]:
        """
        Send ``body`` without waiting; one future per part.

        Text over 67 bytes is cut (``Truncate``) or split on word boundaries
        (``Continue``). With ``ack``, each future resolves to the ack frame,
        or fails with MessageRejected or MaximumRetryExceeded. Without
        ``ack``, the futures are already resolved to None.
        """
        return [
            future
            for future, _ in self._submit_parts(recipient, body, ack, overflow, path)
        ]

    def _submit_parts(
        self,
        recipient: str,
        body: str,
        ack: bool,
        overflow: OverflowDisposition,
        path: list[Address] | str | None,
    ) -> list[tuple[asyncio.Future, Message]]:
        if overflow == OverflowDisposition.Continue:
            parts = split_message(body)
        else:
            parts = [truncate_message(body)]
        return [self._submit_part(recipient, part, ack, path) for part in parts]

    def _submit_part(
        self, recipient: str, text: str, ack: bool, path: list[Address] | str | None
    ) -> tuple[asyncio.Future, Message]:
        future = asyncio.get_running_loop().create_future()
        frame_path = path_split(path) if path else self.default_path
        if not ack:
            message = self._message(recipient, text, None)
            self.write(self.prepare_frame(message, frame_path))
            future.set_result(None)
            return future, message
        number = self._burn_message_sequence_number()
        field = number
        if self.reply_ack:
            field = (
                number + b"}" + self._pending_reply_acks.pop(str(recipient).upper(), b"")
            )
        message = self._message(recipient, text, field)
        om = OutstandingMessage(
            message=message,
            path=frame_path,
            future=future,
            remaining_tries=self.retries,
            period=self.retry_period,
        )
        self.outstanding[number] = om
        try:
            self.write(self.prepare_frame(message, frame_path))
        except NotConnected:
            logger.warning(
                "%s: not connected; will retry message %s", self.mycall, number
            )
        om.timer = asyncio.get_running_loop().call_later(om.period, self._retry, number)
        return future, message

    async def send_message(
        self,
        recipient: str,
        body: str,
        ack: bool = False,
        overflow: OverflowDisposition = OverflowDisposition.Truncate,
        path: list[Address] | str | None = None,
    ) -> list[APRSFrame | None]:
        """
        Send ``body`` and wait until every part is acked (with ``ack``).

        Returns the ack frame for each part (None without ``ack``). Raises
        MessageRejected or MaximumRetryExceeded.
        """
        futures = self.submit_message(recipient, body, ack, overflow, path)
        return list(await asyncio.gather(*futures))

    def _retry(self, number: bytes) -> None:
        om = self.outstanding.get(number)
        if om is None or om.future.done():
            return
        if om.remaining_tries <= 0:
            self._finish(om)
            om.future.set_exception(
                MaximumRetryExceeded(f"{om.recipient} did not acknowledge {om.message}")
            )
            return
        om.remaining_tries -= 1
        try:
            self.write(self.prepare_frame(om.message, om.path))
        except NotConnected:
            logger.warning(
                "%s: not connected; message %s retry skipped", self.mycall, number
            )
        om.timer = asyncio.get_running_loop().call_later(om.period, self._retry, number)

    def _finish(self, om: OutstandingMessage) -> None:
        if om.timer is not None:
            om.timer.cancel()
        for number, value in list(self.outstanding.items()):
            if value is om:
                del self.outstanding[number]

    def _resolve(self, number: bytes, frame: APRSFrame, rejected: bool) -> None:
        om = self.outstanding.get(number)
        if om is None or om.future.done():
            return
        if not _same_station(om.recipient, str(frame.source)):
            return  # someone else's ack for the same number
        self._finish(om)
        if rejected:
            om.future.set_exception(MessageRejected(frame))
        else:
            om.future.set_result(frame)

    def send_status(
        self, status: str, path: list[Address] | str | None = None
    ) -> APRSFrame:
        frame = self.prepare_frame(
            StatusReport(
                raw=b"", data_type=DataType.STATUS, data=b"", status=status.encode()
            ),
            path,
        )
        self.write(frame)
        return frame

    def send_position(
        self, position: Position, path: list[Address] | str | None = None
    ) -> APRSFrame:
        frame = self.prepare_frame(
            PositionReport(
                raw=b"",
                data_type=DataType.POSITION_W_O_TIMESTAMP_MSG,
                data=b"",
                position=position,
            ),
            path,
        )
        self.write(frame)
        return frame

    # -- delayed send queue --

    async def schedule(
        self,
        frame: APRSFrame | InformationField,
        delay: float = 0.0,
        at: t.Any = None,
        path: list[Address] | str | None = None,
    ) -> "PacketRecord":
        """
        Queue ``frame`` (or an info field, framed from ``mycall``) in the
        store, to be transmitted ``delay`` seconds from now or ``at``. The
        queue survives restarts. Scheduled messages are not ack-tracked.
        """
        if self.store is None:
            raise RuntimeError("schedule() needs a PacketStore (store=...)")
        if not isinstance(frame, APRSFrame):
            frame = self.prepare_frame(frame, path)
        record = await self.store.schedule(frame, delay=delay, at=at)
        if self._wakeup is not None:
            self._wakeup.set()
        return record

    async def _run_sender(self) -> None:
        from .store import utcnow  # the db extra is optional

        store = self.store
        assert store is not None
        while not self._closing:
            self._wakeup.clear()
            timeout: float | None = None
            try:
                if self.connected:
                    for record in await store.due():
                        if not self.connected:
                            break
                        if not await store.claim(record):
                            continue  # another sender took it
                        try:
                            self.write(record.frame, record=False)
                        except Exception as exc:  # pylint: disable=broad-except
                            await store.mark_error(record, str(exc))
                    next_due = await store.next_due()
                    if next_due is not None:
                        timeout = max(0.0, (next_due - utcnow()).total_seconds())
            except Exception:  # pylint: disable=broad-except
                logger.exception("Send queue failed; retrying")
                timeout = 1.0
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout)
