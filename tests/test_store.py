import asyncio
import datetime

import pytest
from aprs import APRSFrame

from base_aprs_client.store import PacketRecord, PacketState, PacketStore, utcnow

STATUS = "N0CALL-1>APZ069,WIDE1-1:>hello world"
MESSAGE = "N0CALL-1>APZ069::N0CALL-2 :ping{1"


@pytest.fixture(params=["memory", "file"])
async def store(request, tmp_path):
    url = (
        "sqlite+aiosqlite:///:memory:"
        if request.param == "memory"
        else f"sqlite+aiosqlite:///{tmp_path / 'packets.db'}"
    )
    async with PacketStore(url) as s:
        yield s


async def test_record_received_and_sent(store):
    rx = await store.record_received(APRSFrame.from_str(STATUS))
    tx = await store.record_sent(MESSAGE)
    assert rx.id is not None and tx.id is not None
    assert rx.state is PacketState.RECEIVED
    assert tx.state is PacketState.SENT and tx.sent == tx.created

    history = await store.history()
    assert [r.id for r in history] == [tx.id, rx.id]
    assert [r.id for r in await store.history(state=PacketState.RECEIVED)] == [rx.id]
    assert str(history[1].frame) == STATUS


async def test_tnc2_round_trips_exactly(store):
    # trailing spaces and non-ASCII bytes in the info field are significant
    tnc2 = "N0CALL-1>APZ069::N0CALL-2 :trailing spaces   "
    record = await store.record_received(tnc2)
    fetched = await store.get(record.id)
    assert fetched is not None
    assert fetched.tnc2 == tnc2
    assert str(fetched.frame) == tnc2


async def test_datetimes_are_aware_utc(store):
    naive = datetime.datetime(2024, 1, 2, 3, 4, 5)
    pacific = datetime.datetime(
        2024, 1, 1, 19, 4, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=-8))
    )
    a = await store.record_received(STATUS, when=naive)
    b = await store.record_received(STATUS, when=pacific)
    for record in (await store.get(a.id), await store.get(b.id)):
        assert record is not None
        assert record.created == naive.replace(tzinfo=datetime.UTC)
        assert record.created.tzinfo is datetime.UTC


async def test_schedule_due_and_scheduled(store):
    now = utcnow()
    later = await store.schedule(STATUS, delay=60)
    soon = await store.schedule(MESSAGE, delay=datetime.timedelta(seconds=30))
    past = await store.schedule(MESSAGE, at=now - datetime.timedelta(seconds=5))

    assert [r.id for r in await store.due(now)] == [past.id]
    assert [r.id for r in await store.scheduled(now)] == [soon.id, later.id]
    assert await store.next_due() == past.due

    in_45s = now + datetime.timedelta(seconds=45)
    assert [r.id for r in await store.due(in_45s)] == [past.id, soon.id]
    assert [r.id for r in await store.scheduled(in_45s)] == [later.id]


async def test_due_ties_are_fifo(store):
    at = utcnow() - datetime.timedelta(seconds=1)
    records = [await store.schedule(f"N0CALL>APZ069:>{i}", at=at) for i in range(5)]
    assert [r.id for r in await store.due()] == [r.id for r in records]


async def test_empty_queue(store):
    assert await store.next_due() is None
    assert await store.due() == []
    assert await store.scheduled() == []


async def test_transitions(store):
    a = await store.schedule(STATUS)
    b = await store.schedule(STATUS)
    c = await store.schedule(STATUS)

    assert await store.claim(a)
    assert a.state is PacketState.SENT and a.sent is not None
    assert not await store.claim(a), "a record is only sent once"

    assert await store.cancel(b.id)
    assert not await store.claim(b.id), "cancelled records are not sent"
    fetched_b = await store.get(b.id)
    assert fetched_b is not None and fetched_b.state is PacketState.CANCELLED

    assert await store.mark_error(c, "transport closed")
    fetched_c = await store.get(c.id)
    assert fetched_c is not None
    assert fetched_c.state is PacketState.ERROR
    assert fetched_c.error == "transport closed"

    assert await store.due() == []
    assert not await store.cancel(9999)


async def test_concurrent_claims_send_once(store):
    record = await store.schedule(STATUS)
    results = await asyncio.gather(*(store.claim(record.id) for _ in range(10)))
    assert results.count(True) == 1


async def test_history_filters(store):
    old = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    await store.record_received(STATUS, when=old)
    new = await store.record_received(STATUS)
    await store.record_sent(STATUS)
    since = await store.history(since=old + datetime.timedelta(days=1))
    assert len(since) == 2
    assert [r.id for r in await store.history(limit=1, state=PacketState.RECEIVED)] == [
        new.id
    ]


async def test_persists_across_reopen(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'p.db'}"
    async with PacketStore(url) as store:
        queued = await store.schedule(MESSAGE, delay=3600)
    async with PacketStore(url) as store:
        (record,) = await store.scheduled()
        assert record.id == queued.id
        assert record.due == queued.due
        assert record.tnc2 == MESSAGE


async def test_record_is_a_dataclass():
    record = PacketRecord(tnc2=STATUS, state=PacketState.RECEIVED)
    assert record.id is None and record.due is None
    assert record.created.tzinfo is datetime.UTC
    assert "PacketRecord(" in repr(record)
