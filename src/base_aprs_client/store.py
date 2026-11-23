"""
Persistent packet log and delayed send queue.

Every frame the client hears or sends can be recorded as a ``PacketRecord``,
and frames can be queued for transmission at a later time. Records are
stored with SQLAlchemy 2.0 (dataclass-mapped models) behind an async
session, so the database driver handles concurrency rather than the
client. SQLite via aiosqlite is the default; any async SQLAlchemy URL
works.

Install with the ``db`` extra: ``pip install base-aprs-client[db]``.
"""

import datetime
import enum
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager

from aprs import APRSFrame
from sqlalchemy import DateTime, Index, String, TypeDecorator, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedAsDataclass, mapped_column

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _as_utc(when: datetime.datetime) -> datetime.datetime:
    """Naive datetimes are taken to be UTC already."""
    if when.tzinfo is None:
        return when.replace(tzinfo=datetime.UTC)
    return when.astimezone(datetime.UTC)


def _due(delay: float | datetime.timedelta, at: datetime.datetime | None):
    if at is not None:
        return _as_utc(at)
    if not isinstance(delay, datetime.timedelta):
        delay = datetime.timedelta(seconds=delay)
    return utcnow() + delay


class PacketState(enum.Enum):
    RECEIVED = "received"
    QUEUED = "queued"
    CANCELLED = "cancelled"
    ERROR = "error"
    SENT = "sent"


class UTCDateTime(TypeDecorator):
    """
    Timezone-aware UTC datetimes on any backend.

    SQLite has no timezone-aware type, so values are stored as naive UTC and
    returned aware.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return _as_utc(value).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=datetime.UTC)


class Base(MappedAsDataclass, DeclarativeBase):
    type_annotation_map = {datetime.datetime: UTCDateTime}


class PacketRecord(Base):
    """One frame heard, sent, or waiting to be sent."""

    __tablename__ = "packet"
    __table_args__ = (Index("ix_packet_state_due", "state", "due"),)

    tnc2: Mapped[str] = mapped_column(String)
    """The frame in TNC2 monitor format (APRS-IS text)."""
    state: Mapped[PacketState] = mapped_column(index=True)
    created: Mapped[datetime.datetime] = mapped_column(default_factory=utcnow, index=True)
    """When the frame was heard, sent, or queued."""
    due: Mapped[datetime.datetime | None] = mapped_column(default=None)
    """For QUEUED records, when the frame should be transmitted."""
    sent: Mapped[datetime.datetime | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    id: Mapped[int | None] = mapped_column(primary_key=True, default=None)

    @property
    def frame(self) -> APRSFrame:
        return APRSFrame.from_str(self.tnc2)


class PacketStore:
    """
    Async packet log and delayed send queue.

    ::

        async with PacketStore("sqlite+aiosqlite:///N0CALL.db") as store:
            await store.record_received(frame)
            await store.schedule(reply, delay=10)
            for record in await store.due():
                if not await store.claim(record):
                    continue  # another sender took it
                try:
                    transmit(record.frame)
                except OSError as exc:
                    await store.mark_error(record, str(exc))
                    await store.requeue(record, delay=30)  # optional retry

    Claiming before transmitting means a frame is sent at most once per
    record, even with several senders; a failed transmit is then marked
    ERROR and can be queued again with ``requeue``.
    """

    def __init__(
        self,
        url: str = DEFAULT_DATABASE_URL,
        engine: AsyncEngine | None = None,
        **engine_kwargs,
    ) -> None:
        self.engine = engine or create_async_engine(url, **engine_kwargs)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def __aenter__(self) -> "PacketStore":
        await self.create_all()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession]:
        async with self.sessionmaker() as session, session.begin():
            yield session

    async def _add(self, record: PacketRecord) -> PacketRecord:
        async with self.session() as session:
            session.add(record)
        return record

    async def record_received(
        self, frame: APRSFrame | str, when: datetime.datetime | None = None
    ) -> PacketRecord:
        return await self._add(
            PacketRecord(
                tnc2=str(frame),
                state=PacketState.RECEIVED,
                created=_as_utc(when) if when else utcnow(),
            )
        )

    async def record_sent(
        self, frame: APRSFrame | str, when: datetime.datetime | None = None
    ) -> PacketRecord:
        when = _as_utc(when) if when else utcnow()
        return await self._add(
            PacketRecord(tnc2=str(frame), state=PacketState.SENT, created=when, sent=when)
        )

    async def schedule(
        self,
        frame: APRSFrame | str,
        delay: float | datetime.timedelta = 0.0,
        at: datetime.datetime | None = None,
    ) -> PacketRecord:
        """Queue ``frame`` for transmission ``delay`` seconds from now, or ``at``."""
        return await self._add(
            PacketRecord(
                tnc2=str(frame),
                state=PacketState.QUEUED,
                created=utcnow(),
                due=_due(delay, at),
            )
        )

    async def due(self, now: datetime.datetime | None = None) -> Sequence[PacketRecord]:
        """Queued records whose time has come, oldest due first."""
        now = _as_utc(now) if now else utcnow()
        return await self._queued(PacketRecord.due <= now)

    async def scheduled(
        self, now: datetime.datetime | None = None
    ) -> Sequence[PacketRecord]:
        """Queued records still in the future, soonest first."""
        now = _as_utc(now) if now else utcnow()
        return await self._queued(PacketRecord.due > now)

    async def next_due(self) -> datetime.datetime | None:
        """When the next queued record is due, or None if the queue is empty."""
        queued = await self._queued(limit=1)
        return queued[0].due if queued else None

    async def _queued(self, *where, limit: int | None = None) -> Sequence[PacketRecord]:
        stmt = (
            select(PacketRecord)
            .where(PacketRecord.state == PacketState.QUEUED, *where)
            .order_by(PacketRecord.due, PacketRecord.id)
            .limit(limit)
        )
        async with self.session() as session:
            return (await session.scalars(stmt)).all()

    async def _transition(
        self,
        record: PacketRecord | int,
        state: PacketState,
        *conditions,
        **values,
    ) -> bool:
        """
        Move a queue record to ``state`` if it still matches ``conditions``.

        Defaults to records that are still QUEUED. Returns False if the record
        no longer matches (e.g. already sent or cancelled by a concurrent
        caller), so a frame is sent at most once per record.
        """
        record_id = record if isinstance(record, int) else record.id
        if not conditions:
            conditions = (PacketRecord.state == PacketState.QUEUED,)
        async with self.session() as session:
            result = await session.execute(
                update(PacketRecord)
                .where(PacketRecord.id == record_id, *conditions)
                .values(state=state, **values)
                .execution_options(synchronize_session=False)
            )
        changed = bool(result.rowcount)  # ty: ignore[missing-attribute]
        if changed and isinstance(record, PacketRecord):
            record.state = state
            for name, value in values.items():
                setattr(record, name, value)
        return changed

    async def claim(self, record: PacketRecord | int) -> bool:
        """Mark a queued record SENT before transmitting it; False if lost the race."""
        return await self._transition(record, PacketState.SENT, sent=utcnow())

    mark_sent = claim

    async def cancel(self, record: PacketRecord | int) -> bool:
        return await self._transition(record, PacketState.CANCELLED)

    async def mark_error(self, record: PacketRecord | int, error: str) -> bool:
        """
        Record that a queued frame could not be sent.

        Works on a record that is still QUEUED, or one that was claimed (SENT)
        and then failed to transmit. Frames logged with ``record_sent`` were
        never queued and are not affected.
        """
        return await self._transition(
            record,
            PacketState.ERROR,
            PacketRecord.state.in_([PacketState.QUEUED, PacketState.SENT]),
            PacketRecord.due.is_not(None),
            error=error,
        )

    async def requeue(
        self,
        record: PacketRecord | int,
        delay: float | datetime.timedelta = 0.0,
        at: datetime.datetime | None = None,
    ) -> bool:
        """
        Queue a failed (ERROR) record again, ``delay`` seconds from now or ``at``.

        Clears ``error`` and ``sent``. Returns False if the record is not in
        ERROR.
        """
        return await self._transition(
            record,
            PacketState.QUEUED,
            PacketRecord.state == PacketState.ERROR,
            due=_due(delay, at),
            error=None,
            sent=None,
        )

    async def history(
        self,
        state: PacketState | None = None,
        since: datetime.datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[PacketRecord]:
        """Logged records, newest first."""
        stmt = select(PacketRecord).order_by(
            PacketRecord.created.desc(), PacketRecord.id.desc()
        )
        if state is not None:
            stmt = stmt.where(PacketRecord.state == state)
        if since is not None:
            stmt = stmt.where(PacketRecord.created >= _as_utc(since))
        async with self.session() as session:
            return (await session.scalars(stmt.limit(limit))).all()

    async def get(self, record_id: int) -> PacketRecord | None:
        async with self.session() as session:
            return await session.get(PacketRecord, record_id)
