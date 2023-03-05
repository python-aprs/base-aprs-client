import contextlib
from enum import Enum
import os
import time
from typing import Iterator, List, Optional, Sequence

import attrs
from sqlmodel import Field, Session, SQLModel, create_engine, select

from aprs import APRSFrame, InformationField, TCP
from ax253 import Address

from .client import Client


class PacketState(Enum):
    RECEIVED = "received"
    QUEUED = "queued"
    CANCELLED = "cancelled"
    ERROR = "error"
    SENT = "sent"


class PacketRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tnc2: str = Field(index=True)
    state: PacketState = Field(index=True)
    timestamp: float = Field(default=time.time, index=True)


@attrs.define
class Channel(Client):
    database_url = attrs.field(default=f"sqlite:///database.db")
    _engine_kwargs = attrs.field(factory=dict)
    engine = attrs.field()
    default_delay = attrs.field(default=0.0)

    @engine.default
    def _engine_default(self):
        return create_engine(self.database_url, **self._engine_kwargs)

    def __attrs_post_init__(self):
        self.create_db_and_tables()

    def create_db_and_tables(self) -> None:
        SQLModel.metadata.create_all(self.engine)

    @contextlib.contextmanager
    def session(self, commit=True):
        with Session(self.engine) as session:
            yield session
            if commit:
                session.commit()

    def on_frame(self, frame: APRSFrame) -> None:
        with self.session() as session:
            session.add(
                PacketRecord(
                    tnc2=str(frame),
                    state=PacketState.RECEIVED,
                    timestamp=time.time(),
                ),
            )
        self._step_scheduled_packets()
        return super().on_frame(frame)

    def write(self, frame: APRSFrame, delay: Optional[float] = None) -> None:
        delay = delay or self.default_delay
        if delay > 0.0:
            self.schedule_packet(frame, delay)
        else:
            super().write(frame)
            with self.session() as session:
                session.add(
                    PacketRecord(
                        tnc2=str(frame),
                        state=PacketState.SENT,
                        timestamp=time.time(),
                    ),
                )
        self._step_scheduled_packets()

    def read(
        self,
        min_frames: Optional[int] = -1,
    ) -> Sequence[APRSFrame]:
        self._step_scheduled_packets()
        return super().read(min_frames=min_frames)

    @contextlib.contextmanager
    def delayed(self, delay: float = None):
        orig_default_delay = None
        if delay is not None:
            orig_default_delay = self.default_delay
            self.default_delay = delay
        try:
            yield
        finally:
            if orig_default_delay is not None:
                self.default_delay = orig_default_delay

    def schedule_packet(self, f: APRSFrame, delay: float = 0.0) -> None:
        with self.session() as session:
            session.add(
                PacketRecord(
                    tnc2=str(f),
                    state=PacketState.QUEUED,
                    timestamp=time.time() + delay,
                ),
            )
        self._step_scheduled_packets()

    def _step_scheduled_packets(self) -> None:
        for op in self.outstanding_packets(_from_step=True):
            print("STEP: sending {}".format(op.tnc2))
            self.sync_frame_io.write(APRSFrame.from_str(op.tnc2))
            op.state = PacketState.SENT
            op.timestamp = time.time()

    def outstanding_packets(self, **kwargs) -> Iterator[PacketRecord]:
        with self.session() as session:
            yield from session.exec(
                select(PacketRecord).where(PacketRecord.timestamp <= time.time()).where(PacketRecord.state == PacketState.QUEUED).order_by(PacketRecord.timestamp),
            )

    def scheduled_packets(self) -> Iterator[PacketRecord]:
        with self.session() as session:
            yield from session.exec(
                select(PacketRecord).where(PacketRecord.timestamp > time.time()).where(PacketRecord.state == PacketState.QUEUED).order_by(PacketRecord.timestamp),
            )
            session.commit()


if __name__ == "__main__":
    mycall = os.environ.get("MYCALL", "N7DEM-6")
    with TCP(host="localhost", port=14588) as sync_frame_io:
        channel = Channel(
            mycall=mycall,
            sync_frame_io=sync_frame_io,
            database_url=f"sqlite:///{mycall}.db",
        )
        with channel.delayed(10):
            msg_future = channel.send_message("KF7HVM", "scheduled for 10 seconds in future", ack=True)
        start_time = time.time()
        while time.time() - start_time < 15:
            frames = channel.read(min_frames=-1)
            if frames:
                print(frames)
            time.sleep(1)
        assert msg_future.done()
        msg_future.result()