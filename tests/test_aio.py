"""AsyncClient behaviour against an in-memory protocol."""

import asyncio
import datetime
import logging

import pytest
from aprs import APRSFrame, InformationField, ObjectReport, Position, PositionReport

from base_aprs_client import (
    AsyncClient,
    MaximumRetryExceeded,
    MessageRejected,
    NotConnected,
    OverflowDisposition,
    ReconnectPolicy,
)
from base_aprs_client.common import (
    parse_ack,
    parse_number,
    split_message,
    truncate_message,
)

from .fakes import FakeProtocol, settle

ME = "N0CALL"
PEER = "N0CALL-2"


def incoming(text, source=PEER, to=ME):
    return f"{source}>APRS::{to:<9}:{text}"


@pytest.fixture
async def proto():
    return FakeProtocol()


@pytest.fixture
async def client(proto):
    c = AsyncClient(
        ME,
        default_path="WIDE1-1",
        reconnect=None,
        next_message_sequence_number=0x2A,
        retry_period=0.05,
        retries=2,
        reply_ack=False,
    )
    await c.start(proto)
    yield c
    await c.close()


# --- sending -------------------------------------------------------------------


async def test_truncate_is_the_default(client, proto):
    await client.send_message(PEER, "x" * 80)
    assert proto.written == [f"{ME}>APZ069,WIDE1-1::{PEER} :" + "x" * 67]


async def test_truncate_does_not_split_utf8(client, proto):
    await client.send_message(PEER, "x" * 66 + "é")  # 68 bytes
    assert proto.written[0].endswith(":" + "x" * 66)


async def test_continue_uses_requested_path_and_word_breaks(client, proto):
    body = "word " * 15
    await client.send_message(
        PEER, body, overflow=OverflowDisposition.Continue, path=["WIDE2-1"]
    )
    assert proto.written == [
        f"{ME}>APZ069,WIDE2-1::{PEER} :" + "word " * 13,
        f"{ME}>APZ069,WIDE2-1::{PEER} :" + "word " * 2,
    ]


async def test_submit_without_ack_is_resolved(client):
    (future,) = client.submit_message(PEER, "hi")
    assert future.done() and future.result() is None


async def test_status_and_position(client, proto):
    client.send_status("on the air")
    client.send_position(Position(lat=49.5, long=-72.75, symbol_code=b">"))
    assert proto.written == [
        f"{ME}>APZ069,WIDE1-1:>on the air",
        f"{ME}>APZ069,WIDE1-1:=4930.00N/07245.00W>",
    ]


async def test_write_when_disconnected(client, proto):
    proto.drop()
    await settle()
    assert not client.connected
    with pytest.raises(NotConnected):
        client.send_status("x")


# --- acks, rejects, retries ------------------------------------------------------


async def test_ack_resolves(client, proto):
    task = asyncio.ensure_future(client.send_message(PEER, "hi", ack=True))
    await settle()
    assert proto.written == [f"{ME}>APZ069,WIDE1-1::{PEER} :hi{{2a"]
    proto.feed(incoming("ack2a"))
    (ack,) = await asyncio.wait_for(task, 1)
    assert ack.info.text == b"ack2a"
    assert client.outstanding == {}


async def test_rej_raises(client, proto):
    (future,) = client.submit_message(PEER, "hi", ack=True)
    proto.feed(incoming("rej2a"))
    with pytest.raises(MessageRejected) as excinfo:
        await asyncio.wait_for(future, 1)
    assert str(excinfo.value.frame.source) == PEER


async def test_ack_from_another_station_is_ignored(client, proto):
    (future,) = client.submit_message(PEER, "hi", ack=True)
    proto.feed(incoming("ack2a", source="N0CALL-9"))
    await settle()
    assert not future.done()


@pytest.mark.parametrize(
    "text", ["acknowledged", "ack 2a", "ack", "rejoice2a", "ack2b", "ack2a{5"]
)
async def test_not_an_ack(client, proto, text):
    (future,) = client.submit_message(PEER, "hi", ack=True)
    proto.feed(incoming(text))
    await settle()
    assert not future.done()


async def test_retries_on_a_timer_then_gives_up(client, proto):
    """Retries need no reads: the loop's timers drive them."""
    (future,) = client.submit_message(PEER, "anyone?", ack=True)
    with pytest.raises(MaximumRetryExceeded):
        await asyncio.wait_for(future, 2)
    assert proto.written == [f"{ME}>APZ069,WIDE1-1::{PEER} :anyone?{{2a"] * 3
    assert client.outstanding == {}


async def test_ack_after_a_retry(client, proto):
    (future,) = client.submit_message(PEER, "hi", ack=True)
    await asyncio.sleep(0.07)
    assert len(proto.written) == 2
    proto.feed(incoming("ack2a"))
    await asyncio.wait_for(future, 1)
    await asyncio.sleep(0.1)
    assert len(proto.written) == 2, "no retries after the ack"


async def test_retries_survive_a_disconnect(proto):
    """A retry while disconnected is skipped, not fatal; it resumes after reconnect."""
    second = FakeProtocol()

    async def connector():
        return second

    c = AsyncClient(
        ME,
        connector=connector,
        reconnect=ReconnectPolicy(initial_delay=0.08, jitter=0),
        retry_period=0.05,
        # enough tries that the last one can't race the ack fed at ~0.2s
        retries=10,
        reply_ack=False,
    )
    await c.start(proto)
    (future,) = c.submit_message(PEER, "hi", ack=True)
    proto.drop()
    await asyncio.sleep(0.2)
    assert c.connected and c.protocol is second
    assert second.written, "retried on the new connection"
    second.feed(incoming("ack" + second.written[0].rsplit("{", 1)[1]))
    await asyncio.wait_for(future, 1)
    await c.close()


# --- incoming messages -----------------------------------------------------------


async def test_incoming_message_is_acked_and_delivered(client, proto):
    got = []
    client.on_message(lambda message, frame: got.append(message.text))
    proto.feed(incoming("hello{7"))
    frame = await client.recv(1)
    assert frame.info.text == b"hello"
    assert got == [b"hello"]
    assert proto.written == [f"{ME}>APZ069,WIDE1-1::{PEER} :ack7"]


async def test_async_handlers_are_awaited(client, proto):
    got = asyncio.Event()

    @client.on_frame
    async def handler(frame):
        got.set()

    proto.feed(incoming("hi"))
    await asyncio.wait_for(got.wait(), 1)


async def test_failing_handler_is_logged(client, proto, caplog):
    client.on_frame(lambda frame: 1 / 0)
    with caplog.at_level(logging.ERROR):
        proto.feed(incoming("hi"))
        await client.recv(1)
    assert "failed" in caplog.text


async def test_duplicates_are_acked_but_delivered_once(client, proto):
    for _ in range(3):
        proto.feed(incoming("hello{7"))
    proto.feed(incoming("plain"))
    proto.feed(incoming("plain"))
    await settle(10)
    frames = client.recv_nowait()
    assert [f.info.text for f in frames] == [b"hello", b"plain"]
    assert proto.written == [f"{ME}>APZ069,WIDE1-1::{PEER} :ack7"] * 3


async def test_duplicate_window_expires(proto):
    c = AsyncClient(ME, reconnect=None, duplicate_window=0.05)
    await c.start(proto)
    proto.feed(incoming("hello{7"))
    await settle()
    await asyncio.sleep(0.07)
    proto.feed(incoming("hello{7"))
    await settle()
    assert len(c.recv_nowait()) == 2
    await c.close()


async def test_other_traffic_is_delivered_not_acked(client, proto):
    proto.feed("N0CALL-3>APRS:>status")
    proto.feed(incoming("hi{1", to="N0CALL-4"))
    await settle()
    assert len(client.recv_nowait()) == 2
    assert proto.written == []


# --- reply-ack -------------------------------------------------------------------


@pytest.fixture
async def rclient(proto):
    c = AsyncClient(ME, reconnect=None, next_message_sequence_number=0x2A, retry_period=5)
    await c.start(proto)
    yield c
    await c.close()


async def test_reply_ack_advertised(rclient, proto):
    rclient.submit_message(PEER, "hi", ack=True)
    assert proto.written == [f"{ME}>APZ069::{PEER} :hi{{2a}}"]


async def test_reply_ack_acks_outstanding(rclient, proto):
    (future,) = rclient.submit_message(PEER, "hi", ack=True)
    # the peer's own message piggybacks the ack for 2a
    proto.feed(incoming("hello back{3B}2a"))
    ack = await asyncio.wait_for(future, 1)
    assert ack.info.text == b"hello back"
    # and its message 3B is acked in reply-ack style
    assert proto.written[-1] == f"{ME}>APZ069::{PEER} :ack3B}}"


async def test_standalone_reply_ack_style_ack(rclient, proto):
    (future,) = rclient.submit_message(PEER, "hi", ack=True)
    proto.feed(incoming("ack2a}"))
    await asyncio.wait_for(future, 1)


async def test_reply_ack_piggybacks_pending_ack(rclient, proto):
    proto.feed(incoming("question{3B}"))
    await settle()
    rclient.submit_message(PEER, "answer", ack=True)
    assert proto.written[-1] == f"{ME}>APZ069::{PEER} :answer{{2a}}3B"
    rclient.submit_message(PEER, "more", ack=True)
    assert proto.written[-1] == f"{ME}>APZ069::{PEER} :more{{2b}}", "piggybacked once"


# --- lifecycle -------------------------------------------------------------------


async def test_async_iteration_ends_on_close(client, proto):
    proto.feed(incoming("one"))
    frames = []

    async def consume():
        async for frame in client:
            frames.append(frame)

    task = asyncio.ensure_future(consume())
    await settle()
    await client.close()
    await asyncio.wait_for(task, 1)
    assert len(frames) == 1
    with pytest.raises(EOFError):
        await client.recv(1)


async def test_close_cancels_outstanding(client, proto):
    (future,) = client.submit_message(PEER, "hi", ack=True)
    await client.close()
    assert future.cancelled()
    assert proto.transport.closed
    await client.close()  # idempotent


async def test_reconnect_backoff_and_give_up(proto, caplog):
    attempts = []

    async def connector():
        attempts.append(asyncio.get_running_loop().time())
        raise ConnectionRefusedError("down")

    c = AsyncClient(
        ME,
        connector=connector,
        reconnect=ReconnectPolicy(initial_delay=0.01, factor=2, jitter=0, max_attempts=3),
    )
    await c.start(proto)
    with caplog.at_level(logging.WARNING):
        proto.drop()
        with pytest.raises(EOFError):
            await c.recv(2)
    assert len(attempts) == 3
    assert "giving up" in caplog.text
    await c.close()


def test_reconnect_policy_delays():
    policy = ReconnectPolicy(
        initial_delay=1, factor=3, max_delay=5, jitter=0, max_attempts=4
    )
    assert list(policy.delays()) == [1, 3, 5, 5]
    jittered = list(
        ReconnectPolicy(initial_delay=10, jitter=0.1, max_attempts=50).delays()
    )
    assert all(9 <= d for d in jittered)


async def test_start_needs_a_connector():
    with pytest.raises(ValueError):
        await AsyncClient(ME).start()


async def test_dropped_frames_when_nobody_reads(proto):
    c = AsyncClient(ME, reconnect=None, max_queued_frames=2)
    await c.start(proto)
    for i in range(4):
        proto.feed(f"N0CALL-3>APRS:>{i}")
    await settle(10)
    assert [bytes(f.info) for f in c.recv_nowait()] == [b">2", b">3"]
    assert c.dropped_frames == 2
    await c.close()


# --- helpers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, chunks",
    [
        ("", [""]),
        ("a" * 67, ["a" * 67]),
        ("a" * 68, ["a" * 67, "a"]),
        ("word " * 14, ["word " * 13, "word "]),
        (" " + "c" * 70, [" " + "c" * 66, "c" * 4]),
        ("é" * 40, ["é" * 33, "é" * 7]),
    ],
)
def test_split_message(text, chunks):
    assert split_message(text) == chunks
    assert "".join(chunks) == text


def test_truncate_message():
    assert truncate_message("a" * 70) == "a" * 67
    assert truncate_message("é" * 40) == "é" * 33


@pytest.mark.parametrize(
    "text, expected",
    [
        (b"ack1", (b"ack", b"1")),
        (b"rejABCDE", (b"rej", b"ABCDE")),
        (b"ackAB}", (b"ack", b"AB")),
        (b"ackAB}CD", (b"ack", b"AB")),
        (b"ackABCDEF", None),
        (b"Ack1", None),
    ],
)
def test_parse_ack(text, expected):
    assert parse_ack(text) == expected


@pytest.mark.parametrize(
    "number, expected",
    [
        (b"2a", (b"2a", False, None)),
        (b"2a}", (b"2a", True, None)),
        (b"2a}1B", (b"2a", True, b"1B")),
        (b"weird!", (b"weird!", False, None)),
    ],
)
def test_parse_number(number, expected):
    assert parse_number(number) == expected


@pytest.mark.parametrize("path", [None, "WIDE1-1,WIDE2-1"])
async def test_frames_encode_as_ax25(client, path):
    """The last address carries the end-of-address bit, so TNCs can decode it."""
    frame = client.prepare_frame(InformationField.from_bytes(b">status"), path)
    assert str(APRSFrame.from_bytes(bytes(frame))) == str(frame)


async def test_acks_are_not_passed_to_on_message(client, proto):
    """base-aprs-client#27"""
    messages, frames = [], []
    client.on_message(lambda message, frame: messages.append(message.text))
    client.on_frame(lambda frame: frames.append(frame.info.text))
    (future,) = client.submit_message(PEER, "hi", ack=True)
    proto.feed(incoming("ack2a"))
    proto.feed(incoming("rej99"))
    proto.feed(incoming("a real message"))
    await settle(10)
    assert future.done()
    assert messages == [b"a real message"]
    assert frames == [b"ack2a", b"rej99", b"a real message"]


# --- api-review additions ----------------------------------------------------------


async def test_cancelling_a_message_stops_retries(client, proto):
    """#22: future.cancel() is the whole cancel API."""
    (future,) = client.submit_message(PEER, "hi", ack=True)
    assert len(client.outstanding) == 1
    future.cancel()
    await settle()
    assert client.outstanding == {}
    await asyncio.sleep(0.15)  # past several retry periods
    assert len(proto.written) == 1


async def test_on_transmit(client, proto):
    """#23"""
    sent = []
    client.on_transmit(sent.append)
    client.send_status("hello")
    proto.feed(incoming("ping{3"))  # our ack is transmitted too
    await client.recv(1)
    assert [str(f) for f in sent] == proto.written


async def test_messages_skip_acks_and_duplicates(client, proto):
    """#23"""
    got = []

    async def collect():
        async for frame in client.messages():
            got.append(frame.info.text)

    task = asyncio.ensure_future(collect())
    await settle()
    (future,) = client.submit_message(PEER, "out", ack=True)
    proto.feed(incoming("ack2a"))  # an ack: not a message for the app
    proto.feed(incoming("one{1"))
    proto.feed(incoming("one{1"))  # duplicate
    proto.feed(incoming("two", to="N0CALL-9"))  # not to me
    proto.feed(incoming("three"))
    await settle(10)
    assert future.done()
    await client.close()
    await asyncio.wait_for(task, 1)
    assert got == [b"one", b"three"]


async def test_reply(client, proto):
    """#23"""
    proto.feed(incoming("ping"))
    frame = await client.recv(1)
    await client.reply(frame, "pong")
    assert proto.written[-1] == f"{ME}>APZ069,WIDE1-1::{PEER} :pong"


async def test_send_position_options(client, proto):
    """#24"""
    position = Position(lat=49.5, long=-72.75, symbol_code=b">")
    client.send_position(position, comment="mobile")
    client.send_position(position, messaging=False)
    when = datetime.datetime(2026, 11, 9, 23, 45, tzinfo=datetime.UTC)
    client.send_position(position, timestamp=when, comment="t")
    client.send_position(position, timestamp=when, messaging=False)
    assert proto.written == [
        f"{ME}>APZ069,WIDE1-1:=4930.00N/07245.00W>mobile",
        f"{ME}>APZ069,WIDE1-1:!4930.00N/07245.00W>",
        f"{ME}>APZ069,WIDE1-1:@092345z4930.00N/07245.00W>t",
        f"{ME}>APZ069,WIDE1-1:/092345z4930.00N/07245.00W>",
    ]


async def test_send_position_timestamp_now(client, proto):
    client.send_position(Position(lat=1, long=2), timestamp=True)
    info = APRSFrame.from_str(proto.written[0]).info
    assert isinstance(info, PositionReport) and info.timestamp is not None
    age = datetime.datetime.now(datetime.UTC) - info.timestamp
    assert datetime.timedelta(0) <= age < datetime.timedelta(minutes=2)


async def test_send_object_item_bulletin(client, proto):
    """#24"""
    position = Position(lat=46.2, long=-122.19, symbol_code=b"+")
    when = datetime.datetime(2026, 11, 9, 23, 45, tzinfo=datetime.UTC)
    client.send_object("AID1", position, "Aid station", timestamp=when)
    client.send_object("AID1", position, killed=True, timestamp=when)
    client.send_item("TENT", position, "first aid")
    client.send_bulletin("Net tonight at 7 PM", id="1")
    assert proto.written == [
        f"{ME}>APZ069,WIDE1-1:;AID1     *092345z4612.00N/12211.40W+Aid station",
        f"{ME}>APZ069,WIDE1-1:;AID1     _092345z4612.00N/12211.40W+",
        f"{ME}>APZ069,WIDE1-1:)TENT!4612.00N/12211.40W+first aid",
        f"{ME}>APZ069,WIDE1-1::BLN1     :Net tonight at 7 PM",
    ]
    info = APRSFrame.from_str(proto.written[0]).info
    assert isinstance(info, ObjectReport) and info.name == b"AID1"


async def test_send_any_info(client, proto):
    """#24"""
    info = InformationField.from_bytes(b">raw status")
    frame = client.send(info, path="WIDE2-2")
    assert str(frame) == proto.written[0] == f"{ME}>APZ069,WIDE2-2:>raw status"


# --- #25: connection URLs ---


@pytest.mark.parametrize(
    ("url", "kind", "args"),
    [
        (
            "aprsis://rotate.aprs2.net:14580?passcode=12345&filter=r/47/-122/50",
            "aprsis",
            ("N0CALL", "rotate.aprs2.net", 14580, "12345", "r/47/-122/50"),
        ),
        ("aprsis://localhost", "aprsis", ("N0CALL", "localhost", 14580, "-1", None)),
        ("kiss+tcp://localhost:8002", "kiss_tcp", ("localhost", 8002, None)),
        ("tcp://localhost", "kiss_tcp", ("localhost", 8001, None)),
        (
            "kiss+serial:///dev/ttyUSB0?baud=1200",
            "kiss_serial",
            ("/dev/ttyUSB0", 1200, None),
        ),
        ("kiss+serial://COM3", "kiss_serial", ("COM3", 9600, None)),
    ],
)
def test_connector_from_url(monkeypatch, url, kind, args):
    from base_aprs_client import aio

    calls = []
    monkeypatch.setattr(aio, f"{kind}_connector", lambda *a: calls.append(a) or "c")
    assert aio.connector_from_url(url, "N0CALL") == "c"
    assert calls == [args]


def test_connector_from_url_overrides(monkeypatch):
    from base_aprs_client import aio

    calls = []
    monkeypatch.setattr(aio, "aprsis_connector", lambda *a: calls.append(a))
    aio.connector_from_url("aprsis://h?passcode=1", "N0CALL", passcode=2, filter="m/5")
    assert calls == [("N0CALL", "h", 14580, "2", "m/5")]


@pytest.mark.parametrize(
    "url", ["http://example.com", "aprsis://", "kiss+tcp://:8001", "kiss+serial://"]
)
def test_connector_from_bad_url(url):
    from base_aprs_client.aio import connector_from_url

    with pytest.raises(ValueError):
        connector_from_url(url, "N0CALL")
