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
    assert "Could not log 1 received frame(s)" in caplog.text
    await store.close()
