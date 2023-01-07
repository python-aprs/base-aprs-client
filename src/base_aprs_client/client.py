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

    def _burn_message_sequence_number(self) -> bytes:
        number = b"%x" % self.next_message_sequence_number
        self.next_message_sequence_number = (
            self.next_message_sequence_number + 1
        ) % SEQ_NUM_MAX
        return number

    def read(
        self,
        min_frames: t.Optional[int] = -1,
    ) -> t.Sequence[APRSFrame]:
        return self.sync_frame_io.read(callback=self.on_frame, min_frames=min_frames)

    def on_message(self, message: Message, frame: APRSFrame) -> None:
        if message.number:
            self.send_message(frame.source, "ack{}".format(message.number.decode()))
        if message.text.startswith(b"ack") or message.text.startswith(b"rej"):
            future = self.outstanding_messages.get(message.text[3:])
            if future:
                future.set_result(frame)

    def on_frame(self, frame: APRSFrame) -> None:
        print("rx: {}".format(frame))
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
    ) -> t.Optional[MessageAckFuture]:
        if ack:
            number = self._burn_message_sequence_number()
            self.outstanding_messages[number] = MessageAckFuture(
                loop=self.sync_frame_io.loop
            )
        else:
            number = None
        if overflow == OverflowDisposition.Continue and len(body) > 67:

            def _chunk(message, sz=67):
                while message:
                    chunk, message = message[:sz], message[sz:]
                    yield self.send_message(recipient, body=chunk, ack=ack)

            return [f for f in _chunk(body)]
        frame = self.send_frame(
            info=Message(
                raw=b"",
                data_type=None,
                data=b"",
                addressee=str(recipient).encode(),
                text=body.encode(),
                number=number,
            ),
        )
        return self.outstanding_messages.get(number)

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
