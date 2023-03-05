from asyncio import Future
from enum import Enum
import random
import time
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
DEFAULT_MESSAGE_RETRY = 3
DEFAULT_MESSAGE_RETRY_PERIOD = 10


class MaximumRetryExceeded(TimeoutError):
    pass


class MessageAckFuture(Future):
    def __init__(self, message, path=None, tries=None, period=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.message = message
        self.path = path
        self.remaining_tries = tries or DEFAULT_MESSAGE_RETRY
        self.period = period or DEFAULT_MESSAGE_RETRY_PERIOD
        self.last_try = time.time()

    def step(self, client):
        if time.time() - self.last_try > self.period:
            if self.remaining_tries > 0:
                self.remaining_tries -= 1
                self.last_try = time.time()
                client.write(client.prepare_frame(info=self.message, path=self.path))
            else:
                # would always wait for at least one more period before raising the error
                self.set_exception(MaximumRetryExceeded("Recipient did not acknowledge {}".format(self.message)))


def path_split(p):
    if isinstance(p, str):
        return [Address.from_any(a.strip()) for a in p.split(",")]
    return [Address.from_any(a) for a in p]


@attrs.define
class Client:
    mycall = attrs.field(converter=Address.from_any)
    sync_frame_io = attrs.field()
    default_path = attrs.field(factory=list, converter=path_split)
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

    def _step_outstanding_messages(self) -> None:
        acked_messages = set()
        for number, outstanding_message in self.outstanding_messages.items():
            if outstanding_message.done():
                acked_messages.add(number)
                if outstanding_message.exception():
                    print(outstanding_message.exception())
            else:
                outstanding_message.step(self)
        for acked_number in acked_messages:
            del self.outstanding_messages[acked_number]

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
        if isinstance(frame.info, Message) and frame.info.addressee.decode() == str(
            self.mycall
        ):
            self.on_message(frame.info, frame)
        self._step_outstanding_messages()

    def prepare_frame(
        self, info: InformationField, path: t.Optional[t.List[Address]] = None,
    ) -> APRSFrame:
        return APRSFrame(
            destination=self.destination,
            source=self.mycall,
            path=path or self.default_path,
            info=info,
        )

    def write(self, frame: APRSFrame) -> None:
        return self.sync_frame_io.write(frame)

    def send_message(
        self,
        recipient: str,
        body: str,
        ack: bool = False,
        overflow: OverflowDisposition = OverflowDisposition.Truncate,
        path: t.Optional[t.List[Address]] = None,
    ) -> t.Optional[MessageAckFuture]:
        if ack:
            number = self._burn_message_sequence_number()
        else:
            number = None
        if overflow == OverflowDisposition.Continue and len(body) > 67:

            def _chunk(message, sz=67):
                while message:
                    chunk, message = message[:sz], message[sz:]
                    yield self.send_message(recipient, body=chunk, ack=ack)

            return [f for f in _chunk(body)]
        message = Message(
            raw=b"",
            data_type=None,
            data=b"",
            addressee=str(recipient).encode(),
            text=body.encode(),
            number=number,
        )
        if number is not None:
            self.outstanding_messages[number] = MessageAckFuture(
                message=message,
                path=path,
                loop=self.sync_frame_io.loop
            )
        frame = self.prepare_frame(
            info=message,
            path=path,
        )
        self.write(frame)
        return self.outstanding_messages.get(number)

    def send_status(self, status: str, path: t.Optional[t.List[Address]] = None) -> APRSFrame:
        frame = self.prepare_frame(
            info=StatusReport(raw=b"", data_type=None, data=b"", status=status.encode()),
            path=path,
        )
        self.write(frame)
        return frame

    def send_position(self, position: Position, path: t.Optional[t.List[Address]] = None) -> APRSFrame:
        frame = self.prepare_frame(
            info=PositionReport(
                raw=b"",
                data_type=DataType.POSITION_W_O_TIMESTAMP_MSG,
                data=b"",
                position=position,
            ),
            path=path,
        )
        self.write(frame)
        return frame
