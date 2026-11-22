import logging
import random
import re
import time
import typing as t
from asyncio import Future
from enum import Enum

import attrs
from aprs import (
    APRSFrame,
    DataType,
    InformationField,
    Message,
    Position,
    PositionReport,
    StatusReport,
)
from ax253 import Address

logger = logging.getLogger(__name__)

# APRS101 p72: "ack" or "rej" and the message number (1-5 characters). A
# reply-ack capable station may append "}" and more (aprs11/replyacks.txt).
ACK_REJ_REX = re.compile(rb"^(ack|rej)([A-Za-z0-9]{1,5})(?:}[A-Za-z0-9]{0,5})?$")


class OverflowDisposition(Enum):
    Truncate = False
    Continue = True


MESSAGE_TEXT_MAX = 67
"""APRS101 p71: longest message text, in bytes, before the "{" message number."""
SEQ_NUM_MAX = 0xFFFF
DEFAULT_MESSAGE_RETRY = 3
DEFAULT_MESSAGE_RETRY_PERIOD = 10


class MaximumRetryExceeded(TimeoutError):
    pass


class MessageRejected(Exception):
    """The recipient answered a message with "rej"."""

    def __init__(self, frame: APRSFrame) -> None:
        super().__init__(f"{frame.source} rejected the message: {frame}")
        self.frame = frame


def parse_ack(text: bytes) -> tuple[bytes, bytes] | None:
    """Return ``(b"ack" or b"rej", number)`` if ``text`` is an ack/rej, else None."""
    match = ACK_REJ_REX.match(text)
    if match is None:
        return None
    return match.group(1), match.group(2)


class MessageAckFuture(Future):
    def __init__(self, message, path=None, tries=None, period=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.message = message
        self.path = path
        self.remaining_tries = DEFAULT_MESSAGE_RETRY if tries is None else tries
        self.period = DEFAULT_MESSAGE_RETRY_PERIOD if period is None else period
        self.last_try = time.time()

    def step(self, client):
        if time.time() - self.last_try > self.period:
            if self.remaining_tries > 0:
                self.remaining_tries -= 1
                self.last_try = time.time()
                client.write(client.prepare_frame(info=self.message, path=self.path))
            else:
                # would always wait for at least one more period before raising the error
                self.set_exception(
                    MaximumRetryExceeded(f"Recipient did not acknowledge {self.message}")
                )


def _utf8_len(text: str) -> int:
    return len(text.encode())


def truncate_message(text: str, size: int = MESSAGE_TEXT_MAX) -> str:
    """Cut ``text`` to at most ``size`` encoded bytes, without splitting a character."""
    while _utf8_len(text) > size:
        text = text[:-1]
    return text


def split_message(text: str, size: int = MESSAGE_TEXT_MAX) -> list[str]:
    """
    Split ``text`` into chunks of at most ``size`` encoded bytes.

    Chunks break after the last space that fits, so words stay whole where
    possible; a word longer than ``size`` is split. Joining the chunks gives
    back ``text`` exactly.
    """
    chunks = []
    while _utf8_len(text) > size:
        head = truncate_message(text, size)
        space = head.rfind(" ")
        if space > 0:
            head = head[: space + 1]
        chunks.append(head)
        text = text[len(head) :]
    if text or not chunks:
        chunks.append(text)
    return chunks


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
                if not outstanding_message.cancelled() and (
                    exc := outstanding_message.exception()
                ):
                    logger.warning("Message %s failed: %s", number.decode(), exc)
            else:
                outstanding_message.step(self)
        for acked_number in acked_messages:
            del self.outstanding_messages[acked_number]

    def read(
        self,
        min_frames: int | None = -1,
    ) -> t.Sequence[APRSFrame]:
        return self.sync_frame_io.read(callback=self.on_frame, min_frames=min_frames)

    def on_message(self, message: Message, frame: APRSFrame) -> None:
        if message.number:
            self.send_message(frame.source, f"ack{message.number.decode()}")
            return  # acks and rejects never carry a message number
        ack = parse_ack(message.text)
        if ack is None:
            return
        kind, number = ack
        future = self.outstanding_messages.get(number)
        if future is None or future.done():
            return
        if kind == b"rej":
            future.set_exception(MessageRejected(frame))
        else:
            future.set_result(frame)

    def on_frame(self, frame: APRSFrame) -> None:
        if isinstance(frame.info, Message) and frame.info.addressee.decode() == str(
            self.mycall
        ):
            self.on_message(frame.info, frame)
        self._step_outstanding_messages()

    def prepare_frame(
        self,
        info: InformationField,
        path: list[Address] | None = None,
    ) -> APRSFrame:
        return APRSFrame(
            destination=self.destination,
            source=self.mycall,
            path=path_split(path) if path else self.default_path,
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
        path: list[Address] | None = None,
    ) -> MessageAckFuture | list[MessageAckFuture | None] | None:
        """
        Send ``body`` to ``recipient``.

        Text longer than 67 bytes is cut (``Truncate``) or split into several
        messages, on word boundaries where possible (``Continue``). A split
        message returns one result per part. With ``ack``, each result is a
        future that resolves when the recipient acks that part.
        """
        if overflow == OverflowDisposition.Continue:
            chunks = split_message(body)
            if len(chunks) > 1:
                return [
                    self.send_message(recipient, body=chunk, ack=ack, path=path)
                    for chunk in chunks
                ]
        else:
            # aprs3 >= 8.1 no longer cuts message text at 67 characters
            body = truncate_message(body)
        number = self._burn_message_sequence_number() if ack else None
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
                message=message, path=path, loop=self.sync_frame_io.loop
            )
        frame = self.prepare_frame(
            info=message,
            path=path,
        )
        self.write(frame)
        return self.outstanding_messages.get(number)

    def send_status(self, status: str, path: list[Address] | None = None) -> APRSFrame:
        frame = self.prepare_frame(
            info=StatusReport(raw=b"", data_type=None, data=b"", status=status.encode()),
            path=path,
        )
        self.write(frame)
        return frame

    def send_position(
        self, position: Position, path: list[Address] | None = None
    ) -> APRSFrame:
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
