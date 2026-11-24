"""End-to-end tests: real clients talking through the APRS-IS simulator."""

import asyncio
import subprocess
import sys
import time

import pytest
from aprs import InformationField, Position

from base_aprs_client import (
    APRSISSimulator,
    AsyncClient,
    Client,
    MaximumRetryExceeded,
    OverflowDisposition,
    ReconnectPolicy,
)
from base_aprs_client.aio import aprsis_connector

Continue = OverflowDisposition.Continue
FAST_RECONNECT = ReconnectPolicy(initial_delay=0.05, max_delay=0.2, jitter=0)


async def eventually(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.01)


def station(sim, call, **kwargs):
    kwargs.setdefault("reconnect", FAST_RECONNECT)
    return AsyncClient(
        mycall=call, connector=aprsis_connector(call, sim.host, sim.port), **kwargs
    )


@pytest.fixture
def sim():
    with APRSISSimulator() as s:
        yield s


async def test_message_and_ack(sim):
    async with station(sim, "ALPHA") as a, station(sim, "BRAVO") as b:
        heard = []
        b.on_message(lambda message, frame: heard.append(message.text))
        await eventually(lambda: sim.n_clients == 2)
        (ack,) = await asyncio.wait_for(
            a.send_message("BRAVO", "hello there", ack=True), 5
        )
        assert str(ack.source) == "BRAVO"
        assert ack.info.text.startswith(b"ack")
        assert heard == [b"hello there"]
        assert a.outstanding == {}


async def test_split_message_acks_every_part(sim):
    async with station(sim, "ALPHA") as a, station(sim, "BRAVO") as b:
        await eventually(lambda: sim.n_clients == 2)
        body = "word " * 40
        acks = await asyncio.wait_for(
            a.send_message("BRAVO", body, ack=True, overflow=Continue), 5
        )
        assert len(acks) == 4
        texts = []
        while len(texts) < 4:
            frame = await b.recv(timeout=5)
            texts.append(frame.info.text)
        assert b"".join(texts).decode().split() == body.split()


async def test_retry_reaches_late_station(sim):
    """The first try goes nowhere; a retry reaches BRAVO once it connects."""
    async with station(sim, "ALPHA", retry_period=0.2, retries=20) as a:
        (future,) = a.submit_message("BRAVO", "are you there", ack=True)
        await asyncio.sleep(0.3)
        assert not future.done()
        async with station(sim, "BRAVO") as b:
            ack = await asyncio.wait_for(future, 5)
            assert str(ack.source) == "BRAVO"
            # BRAVO heard it once, even if several retries arrived
            frames = []
            while not b._frames.empty():
                frames.append(b.recv_nowait())
            assert len(frames) == 1


async def test_retry_gives_up(sim):
    async with station(sim, "ALPHA", retry_period=0.05, retries=2) as a:
        with pytest.raises(MaximumRetryExceeded):
            await asyncio.wait_for(a.send_message("NOBODY", "hello", ack=True), 5)
    tries = [line for _, line in sim.received if b"::NOBODY" in line]
    assert len(tries) == 3


async def test_beacons(sim):
    async with station(sim, "ALPHA") as a, station(sim, "BRAVO") as b:
        await eventually(lambda: sim.n_clients == 2)
        a.send_status("on the air")
        pos = Position(lat=37.5, long=-122.25, sym_table_id=b"/", symbol_code=b"-")
        a.send_position(pos)
        status = await b.recv(timeout=5)
        assert status.info.raw == b">on the air"
        position = await b.recv(timeout=5)
        assert position.info._position.lat == pytest.approx(37.5, abs=0.01)
        assert position.info._position.long == pytest.approx(-122.25, abs=0.01)


async def test_reconnect_keeps_outstanding_messages():
    """The server restarts on the same port; both stations log in again and
    the retry of a message sent before the outage is acked after it."""
    with APRSISSimulator() as first:
        port = first.port
        a = await station(first, "ALPHA", retry_period=0.2, retries=50).start()
        b = await station(first, "BRAVO").start()
        await eventually(lambda: first.n_clients == 2)
        reconnected = []
        a.connect_handlers.append(reconnected.append)
    try:
        await eventually(lambda: not a.connected and not b.connected)
        (future,) = a.submit_message("BRAVO", "after the outage", ack=True)
        with APRSISSimulator(port=port) as second:
            await eventually(lambda: second.n_clients == 2)
            ack = await asyncio.wait_for(future, 5)
            assert str(ack.source) == "BRAVO"
            assert reconnected
    finally:
        await a.close()
        await b.close()


async def test_reconnect_gives_up_and_ends_recv():
    with APRSISSimulator() as s:
        policy = ReconnectPolicy(initial_delay=0.01, jitter=0, max_attempts=2)
        a = await station(s, "ALPHA", reconnect=policy).start()
        # start() returns once connected; let the simulator take the
        # connection before it stops, or the close may never reach ALPHA
        await eventually(lambda: s.n_clients == 1)
    with pytest.raises(EOFError):
        while True:
            await a.recv(timeout=5)
    assert not a.connected
    await a.close()


# --- packet store -------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path):
    pytest.importorskip("sqlalchemy")
    return f"sqlite+aiosqlite:///{tmp_path / 'packets.db'}"


async def test_store_logs_heard_and_sent(sim, db_url):
    from base_aprs_client.store import PacketState, PacketStore

    async with PacketStore(db_url) as store:
        async with (
            station(sim, "ALPHA", store=store) as a,
            station(sim, "BRAVO") as b,
        ):
            await eventually(lambda: sim.n_clients == 2)
            await asyncio.wait_for(a.send_message("BRAVO", "log me", ack=True), 5)
            b.send_status("heard by alpha")
            await a.recv(timeout=5)  # the ack
            await a.recv(timeout=5)  # the status
            await asyncio.sleep(0.1)  # sent frames are logged in the background
        sent = await store.history(PacketState.SENT)
        heard = await store.history(PacketState.RECEIVED)
    assert len(sent) == 1
    assert bytes(sent[0].frame.info).startswith(b":BRAVO    :log me{")
    assert {bytes(r.frame.info.raw)[:1] for r in heard} == {b":", b">"}
    assert any(r.frame.info.raw == b">heard by alpha" for r in heard)


async def test_scheduled_frame_is_sent(sim, db_url):
    from base_aprs_client.store import PacketState, PacketStore

    async with PacketStore(db_url) as store:
        async with (
            station(sim, "ALPHA", store=store) as a,
            station(sim, "BRAVO") as b,
        ):
            await eventually(lambda: sim.n_clients == 2)
            # an info field is framed from ALPHA
            record = await a.schedule(InformationField.from_bytes(b">later"), delay=0.2)
            assert record.state is PacketState.QUEUED
            got = await b.recv(timeout=5)
            assert got.info.raw == b">later"
        assert record.id is not None
        sent = await store.get(record.id)
        assert sent is not None and sent.state is PacketState.SENT


async def test_queue_survives_restart(sim, db_url):
    from base_aprs_client.store import PacketState, PacketStore

    async with PacketStore(db_url) as store:
        async with station(sim, "ALPHA", store=store) as a:
            # make sure the simulator has this ALPHA, so the waits below
            # can't count it as BRAVO
            await eventually(lambda: sim.n_clients == 1)
            record = await a.schedule(a.prepare_frame(b">from the past"), delay=0.5)
    # the first ALPHA is gone before the frame is due; a new one sends it
    await eventually(lambda: sim.n_clients == 0)
    async with PacketStore(db_url) as store:
        async with station(sim, "BRAVO") as b:
            # the frame is already due, so ALPHA sends it as soon as it
            # connects; the simulator must have BRAVO registered by then
            await eventually(lambda: sim.n_clients == 1)
            async with station(sim, "ALPHA", store=store):
                got = await b.recv(timeout=5)
                assert got.info.raw == b">from the past"
        assert record.id is not None
        sent = await store.get(record.id)
        assert sent is not None and sent.state is PacketState.SENT


async def test_schedule_needs_store(sim):
    async with station(sim, "ALPHA") as a:
        with pytest.raises(RuntimeError):
            await a.schedule(a.prepare_frame(b">x"))


def test_import_without_db_extra():
    """The client imports, and runs, without sqlalchemy loaded."""
    code = (
        "import sys, base_aprs_client\n"
        "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy imported'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- sync wrapper, threaded -----------------------------------------------------


def test_threaded_client(sim):
    with (
        Client.aprsis("ALPHA", sim.host, sim.port, reconnect=FAST_RECONNECT) as a,
        Client.aprsis("BRAVO", sim.host, sim.port, reconnect=FAST_RECONNECT) as b,
    ):
        sim.wait_for_clients(2)
        assert a.connected
        (maf,) = a.submit_message("BRAVO", "from a thread")
        ack = maf.result(timeout=5)
        assert str(ack.source) == "BRAVO"
        assert maf.done()
        frames = b.read(min_frames=1)
        assert bytes(frames[0].info).startswith(b":BRAVO    :from a thread{")
        a.send_status("threaded status")
        assert b.read(min_frames=1)[0].info.raw == b">threaded status"


def test_threaded_retry_runs_in_background(sim):
    """Timers fire without the caller driving the loop."""
    with Client.aprsis("ALPHA", sim.host, sim.port, retries=2, retry_period=0.05) as a:
        (maf,) = a.submit_message("NOBODY", "anyone?")
        time.sleep(0.5)
        assert maf.done()
        assert isinstance(maf.exception(), MaximumRetryExceeded)
