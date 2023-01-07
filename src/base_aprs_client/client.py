from asyncio import Future
from enum import Enum
import random
import typing as t

import attrs

from aprs import (
    APRSFrame,
    DataType,
    Message,
    Position,
    InformationField,
    StatusReport,
    PositionReport,
)
from ax253 import Address


class OverflowDisposition(Enum):
    Truncate = False
    Continue = True


SEQ_NUM_MAX = 0xFFFF


class MessageAckFuture(Future):
    pass


@attrs.define
class Client:
    mycall = attrs.field(converter=Address.from_any)
    sync_frame_io = attrs.field()
    destination = attrs.field(default="APZ069", converter=Address.from_any)
    next_message_sequence_number = attrs.field()
    outstanding_messages = attrs.field(factory=dict, init=False)

    @next_message_sequence_number.default
    def _next_message_sequence_number_default(self):
        return random.randint(0, SEQ_NUM_MAX)

    def read(
        self,
        min_frames: t.Optional[int] = -1,
    ) -> t.Sequence[APRSFrame]:
        return self.sync_frame_io.read(callback=self.on_frame, min_frames=min_frames)

    def on_message(self, message: Message, frame: APRSFrame) -> None:
        pass

    def on_frame(self, frame: APRSFrame) -> None:
        print(frame)
        if isinstance(frame.info, Message) and frame.info.addressee.decode() == str(
            self.mycall
        ):
            self.on_message(frame.info, frame)

    def send_frame(
        self, info: InformationField, path: t.Optional[t.List[Address]] = None
    ) -> APRSFrame:
        frame = APRSFrame(
            destination=self.destination,
            source=self.mycall,
            path=path or [],
            info=info,
        )
        self.sync_frame_io.write(frame)
        return frame

    def send_message(
        self,
        recipient: str,
        body: str,
        ack: bool = False,
        overflow: OverflowDisposition = OverflowDisposition.Truncate,
    ) -> MessageAckFuture:
        frame = self.send_frame(
            info=Message(
                raw=b"",
                data_type=None,
                data=b"",
                addressee=str(recipient).encode(),
                text=body.encode(),
            ),
        )

    def send_status(self, status: str) -> APRSFrame:
        return self.send_frame(
            info=StatusReport(raw=b"", data_type=None, data=b"", status=status.encode())
        )

    def send_position(self, position: Position) -> APRSFrame:
        return self.send_frame(
            info=PositionReport(
                raw=b"",
                data_type=DataType.POSITION_W_O_TIMESTAMP_MSG,
                data=b"",
                position=position,
            )
        )
