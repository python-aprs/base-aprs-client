"""AsyncClient logging received frames to a PacketStore."""

import asyncio
from typing import Any

import pytest

from base_aprs_client import AsyncClient

from .fakes import FakeProtocol, settle

pytest.importorskip("sqlalchemy")

from base_aprs_client.store import PacketState, PacketStore  # noqa: E402

ME = "N0CALL"


def status(n: int) -> str:
    return f"N0CALL-{n % 15 + 1}>APZ069:>status {n}"


class GatedStore(PacketStore):
    """A PacketStore whose received-frame writes wait for ``gate``."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.gate = asyncio.Event()
        self.batches: list[int] = []

    async def record_received_many(self, heard):
        heard = list(heard)
        self.batches.append(len(heard))
        await self.gate.wait()
        return await super().record_received_many(heard)


async def received(store: PacketStore) -> list[str]:
    return [r.tnc2 for r in reversed(await store.history(PacketState.RECEIVED))]


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'packets.db'}"


async def test_received_frames_are_logged_in_batches(db_url):
    store = GatedStore(db_url)
    proto: Any = FakeProtocol()
    client = AsyncClient(ME, reconnect=None, store=store)
    await client.start(proto)
    for n in range(5):
        proto.feed(status(n))
    await settle()
    # a slow database doesn't hold up receiving
    assert [str(f) for f in client.recv_nowait()] == [status(n) for n in range(5)]
    for n in range(5, 8):
        proto.feed(status(n))
    await settle()
    store.gate.set()
    await client.close()  # logs everything received before it returns
    assert await received(store) == [status(n) for n in range(8)]
    assert sum(store.batches) == 8 and len(store.batches) < 8
    await store.close()


async def test_log_that_falls_behind_drops_the_oldest(caplog, db_url):
    store = GatedStore(db_url)
    proto: Any = FakeProtocol()
    client = AsyncClient(ME, reconnect=None, store=store, max_queued_frames=2)
    await client.start(proto)
    for n in range(5):
        proto.feed(status(n))
    await settle()
    assert client.unlogged_frames == 3
    assert "Packet log is 2 frames behind" in caplog.text
    store.gate.set()
    await client.close()
    assert await received(store) == [status(3), status(4)]
    await store.close()


async def test_log_failure_does_not_stop_receiving(caplog, db_url):
    class BrokenStore(PacketStore):
        async def record_received_many(self, heard):
            raise RuntimeError("disk full")

    store = BrokenStore(db_url)
    proto: Any = FakeProtocol()
    client = AsyncClient(ME, reconnect=None, store=store)
    await client.start(proto)
    proto.feed(status(1))
    await settle()
    proto.feed(status(2))
    await settle()
    assert [str(f) for f in client.recv_nowait()] == [status(1), status(2)]
    await client.close()
    assert "Could not log 1 of 1 received frame(s)" in caplog.text
    assert client.unlogged_frames == 2
    await store.close()


async def test_close_does_not_break_the_store():
    """close() stops the send-queue loop between queries, never in the middle of one.

    Cancelling a query part way left its connection checked out mid-transaction:
    the default in-memory store lost its table (or hung), and a SQLite file
    stayed locked for the next client.
    """
    store = PacketStore()  # in-memory: one shared connection
    proto: Any = FakeProtocol()
    client = AsyncClient(ME, reconnect=None, store=store)
    await client.start(proto)
    proto.feed(status(1))
    await settle(2)  # the send-queue loop is in its first query now
    await asyncio.wait_for(client.close(), 5)
    assert await asyncio.wait_for(received(store), 5) == [status(1)]
    await store.close()


async def test_frames_waiting_at_start_are_logged(db_url):
    """Frames that arrive before start() finishes setting up the store are logged.

    The reader used to start before the store's queue existed, so a frame
    already waiting (any busy feed) killed the reader while start() awaited
    create_all(): nothing more was received or logged.
    """
    store = PacketStore(db_url)
    proto: Any = FakeProtocol()
    for n in range(3):
        proto.feed(status(n))
    client = AsyncClient(ME, reconnect=None, store=store)
    await client.start(proto)
    proto.feed(status(3))
    await settle()
    assert [str(f) for f in client.recv_nowait()] == [status(n) for n in range(4)]
    await client.close()
    assert await received(store) == [status(n) for n in range(4)]
    await store.close()


async def test_one_bad_frame_loses_only_itself(caplog, db_url):
    class PickyStore(PacketStore):
        async def record_received_many(self, heard):
            heard = list(heard)
            if any("bad" in str(frame) for frame, _ in heard):
                raise ValueError("can't store this one")
            return await super().record_received_many(heard)

    store = PickyStore(db_url)
    proto: Any = FakeProtocol()
    client = AsyncClient(ME, reconnect=None, store=store)
    await client.start(proto)
    for text in ("one", "bad", "three"):
        proto.feed(f"N0CALL-1>APZ069:>{text}")
    await settle()
    await client.close()
    assert await received(store) == ["N0CALL-1>APZ069:>one", "N0CALL-1>APZ069:>three"]
    assert client.unlogged_frames == 1
    assert "Could not log 1 of 3 received frame(s)" in caplog.text
    await store.close()
