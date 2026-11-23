"""
Net control logic, independent of the web interface.

A ``Net`` runs one APRS net on an ``AsyncClient``: stations check in by
sending a message to net control, and each new station gets one reply
("ACK N0CALL, thanks for checking in"). A check-in message may name other
stations too ("checking in with W1AW"); each callsign in it is logged. Net
control can also log check-ins by hand (for voice check-ins), send messages,
and transmit a bulletin, status and position beacon on a timer. Closing the
net sends a summary status.
"""

import asyncio
import csv
import datetime
import io
import logging
import re
from collections import deque
from collections.abc import Callable
from typing import Any

import attrs
from aprs import APRSFrame, Message, Position
from aprs.position import PositionMixin

from base_aprs_client import AsyncClient, MaximumRetryExceeded, MessageRejected
from base_aprs_client.common import OverflowDisposition, parse_ack

log = logging.getLogger(__name__)

CALLSIGN = re.compile(r"\b([A-Z]{1,2}[0-9][A-Z]{1,4}|[0-9][A-Z][0-9][A-Z]{1,3})\b")
"""Amateur callsigns, without SSID: N0CALL, W1AW, KF7HVM, 2E0ABC."""


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def base_call(call: str) -> str:
    """N0CALL-9 -> N0CALL: check-ins are per operator, not per device."""
    return call.upper().split("-", 1)[0]


@attrs.define
class Settings:
    """Everything needed to run a net; the web form edits these."""

    mycall: str = "N0CALL"
    connection: str = "aprsis"
    """One of aprsis, kiss_tcp, kiss_serial."""
    address: str = "rotate.aprs2.net:14580"
    """host:port for aprsis and kiss_tcp, the device for kiss_serial."""
    passcode: str = "-1"
    path: str = "WIDE1-1"
    net_name: str = "APRS Net"
    checkin_reply: str = "ACK {calls}, thanks for checking in to {net}"
    bulletin_group: str = "BLN0"
    bulletin_text: str = ""
    bulletin_minutes: float = 0
    status_text: str = ""
    status_minutes: float = 0
    beacon_lat: float | None = None
    beacon_lon: float | None = None
    beacon_symbol: str = "/r"  # antenna tower
    beacon_minutes: float = 0

    def client(self, **kwargs: Any) -> AsyncClient:
        """An AsyncClient for this connection; not started yet."""
        host, _, port = self.address.rpartition(":")
        if self.connection == "aprsis":
            return _NetClient.aprsis(
                self.mycall, host, int(port or 14580), self.passcode, **kwargs
            )
        if self.connection == "kiss_tcp":
            return _NetClient.kiss_tcp(
                self.mycall, host, int(port or 8001), default_path=self.path, **kwargs
            )
        if self.connection == "kiss_serial":
            return _NetClient.kiss_serial(
                self.mycall, self.address, default_path=self.path, **kwargs
            )
        raise ValueError(f"unknown connection {self.connection!r}")


@attrs.define
class _NetClient(AsyncClient):
    """An AsyncClient that reports every frame it transmits."""

    on_transmit: Callable[[APRSFrame], None] | None = attrs.field(
        default=None, kw_only=True
    )

    def write(self, frame: APRSFrame, record: bool = True) -> None:
        super().write(frame, record)
        if self.on_transmit is not None:
            self.on_transmit(frame)


@attrs.define
class CheckIn:
    callsign: str
    time: datetime.datetime
    text: str
    """What they sent, or "manual" for one net control logged."""
    heard_from: str
    """The station whose message logged this check-in."""


@attrs.define
class Heard:
    time: datetime.datetime
    direction: str  # "rx" or "tx"
    text: str


@attrs.define
class SentMessage:
    time: datetime.datetime
    recipient: str
    text: str
    status: str = "sending"  # then "acked", "rejected" or "no ack"


@attrs.define(eq=False)
class Net:
    """One net, from open() to close()."""

    settings: Settings
    client_options: dict[str, Any] = attrs.field(factory=dict)
    """Extra AsyncClient options, e.g. retry_period in tests."""
    checkins: dict[str, CheckIn] = attrs.field(factory=dict)
    positions: dict[str, tuple[float, float]] = attrs.field(factory=dict)
    heard: deque[Heard] = attrs.field(factory=lambda: deque(maxlen=200))
    sent: list[SentMessage] = attrs.field(factory=list)
    opened: datetime.datetime | None = None
    closed: datetime.datetime | None = None
    version: int = 0
    """Goes up on every change, so a UI can poll cheaply."""
    client: AsyncClient | None = None
    _tasks: list[asyncio.Task] = attrs.field(factory=list)

    @property
    def running(self) -> bool:
        return self.client is not None and self.closed is None

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.connected

    def _changed(self) -> None:
        self.version += 1

    # -- lifecycle --

    async def open(self) -> None:
        """Connect and start answering check-ins and sending timed packets."""
        client = self.settings.client(
            on_transmit=self._transmitted, **self.client_options
        )
        client.on_frame(self._heard)
        client.on_message(self._message)
        await client.start()
        self.client = client
        self.opened, self.closed = now(), None
        s = self.settings
        for minutes, send in [
            (s.bulletin_minutes, self.send_bulletin if s.bulletin_text else None),
            (s.status_minutes, self.send_status if s.status_text else None),
            (s.beacon_minutes, self.send_beacon if s.beacon_lat is not None else None),
        ]:
            if send is not None and minutes > 0:
                self._tasks.append(asyncio.ensure_future(self._every(minutes, send)))
        self._changed()

    async def close(self, summary: bool = True) -> None:
        """Send the summary status (with ``summary``) and disconnect."""
        if self.client is None:
            return
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if summary and self.connected:
            self.client.send_status(self.summary())
        await self.client.close()
        self.closed = now()
        self._changed()

    def summary(self) -> str:
        count = len(self.checkins)
        noun = "check-in" if count == 1 else "check-ins"
        return f"{self.settings.net_name} closed with {count} {noun}"

    async def _every(self, minutes: float, send: Callable[[], Any]) -> None:
        while True:
            try:
                send()
            except Exception:  # noqa: BLE001 - keep the timer going while disconnected
                log.warning("Timed transmission failed", exc_info=True)
            await asyncio.sleep(minutes * 60)

    # -- receiving --

    def _heard(self, frame: APRSFrame) -> None:
        info = frame.info
        if isinstance(info, PositionMixin) and info.lat is not None:
            self.positions[base_call(str(frame.source))] = (
                float(info.lat),
                float(info.long or 0),
            )
        self.heard.append(Heard(now(), "rx", str(frame)))
        self._changed()

    def _transmitted(self, frame: APRSFrame) -> None:
        self.heard.append(Heard(now(), "tx", str(frame)))
        self._changed()

    def _message(self, message: Message, frame: APRSFrame) -> None:
        if message.number is None and parse_ack(message.text):
            return  # an ack for one of ours; the futures in send_message track it
        text = message.text.decode("utf-8", "replace")
        sender = base_call(str(frame.source))
        calls = [sender] + [c for c in CALLSIGN.findall(text.upper()) if c != sender]
        new = [c for c in dict.fromkeys(calls) if c not in self.checkins]
        for call in new:
            self.checkins[call] = CheckIn(call, now(), text, str(frame.source))
        if new:
            reply = self.settings.checkin_reply.format(
                calls=" ".join(new), net=self.settings.net_name
            )
            self.send_message(str(frame.source), reply)
        self._changed()

    # -- net control actions --

    def add_checkin(self, callsign: str, note: str = "manual") -> bool:
        """Log a check-in by hand; False if it's already logged or not a callsign."""
        call = base_call(callsign.strip())
        if not call or call in self.checkins:
            return False
        self.checkins[call] = CheckIn(call, now(), note, self.settings.mycall)
        self._changed()
        return True

    def remove_checkin(self, callsign: str) -> None:
        if self.checkins.pop(callsign, None) is not None:
            self._changed()

    def send_message(self, recipient: str, text: str) -> list[SentMessage]:
        """Send with ack; each part's status updates when the ack comes."""
        assert self.client is not None, "the net isn't open"
        sent = []
        futures = self.client.submit_message(
            recipient.upper(), text, ack=True, overflow=OverflowDisposition.Continue
        )
        for future in futures:
            entry = SentMessage(now(), recipient.upper(), text)
            future.add_done_callback(lambda f, e=entry: self._ack(f, e))
            self.sent.append(entry)
            sent.append(entry)
        self._changed()
        return sent

    def _ack(self, future: asyncio.Future, entry: SentMessage) -> None:
        if future.cancelled():
            entry.status = "cancelled"
        elif isinstance(future.exception(), MessageRejected):
            entry.status = "rejected"
        elif isinstance(future.exception(), MaximumRetryExceeded):
            entry.status = "no ack"
        else:
            entry.status = "acked"
        self._changed()

    def send_bulletin(self) -> None:
        assert self.client is not None
        # bulletins aren't acked: anyone may read them
        self.client.submit_message(
            self.settings.bulletin_group, self.settings.bulletin_text, ack=False
        )

    def send_status(self) -> None:
        assert self.client is not None
        self.client.send_status(self.settings.status_text)

    def send_beacon(self) -> None:
        assert self.client is not None
        s = self.settings
        table, symbol = (s.beacon_symbol + "  ")[:2].encode()
        self.client.send_position(
            Position(
                lat=s.beacon_lat or 0.0,
                long=s.beacon_lon or 0.0,
                sym_table_id=bytes([table]),
                symbol_code=bytes([symbol]),
            )
        )

    def checkins_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["callsign", "time_utc", "message", "heard_from", "lat", "lon"])
        for c in self.checkins.values():
            lat, lon = self.positions.get(c.callsign, ("", ""))
            writer.writerow(
                [
                    c.callsign,
                    c.time.strftime("%Y-%m-%d %H:%M:%S"),
                    c.text,
                    c.heard_from,
                    lat,
                    lon,
                ]
            )
        return out.getvalue()
