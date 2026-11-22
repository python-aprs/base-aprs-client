"""Message helpers shared by the async and sync clients."""

import re
from enum import Enum

from aprs import APRSFrame
from ax253 import Address

MESSAGE_TEXT_MAX = 67
"""APRS101 p71: longest message text, in bytes, before the "{" message number."""
SEQ_NUM_MAX = 0xFFFF
DEFAULT_MESSAGE_RETRY = 3
"""Retransmissions after the first try, when no ack arrives."""
DEFAULT_MESSAGE_RETRY_PERIOD = 10
"""Seconds between tries."""

# APRS101 p72: "ack" or "rej" and the message number (1-5 characters). A
# reply-ack capable station may append "}" and more (aprs11/replyacks.txt).
ACK_REJ_REX = re.compile(rb"^(ack|rej)([A-Za-z0-9]{1,5})(?:}[A-Za-z0-9]{0,5})?$")
# Message number field: "MM", or reply-ack "MM}" / "MM}AA"
NUMBER_REX = re.compile(rb"^([A-Za-z0-9]{1,5})(}([A-Za-z0-9]{0,5}))?$")


class OverflowDisposition(Enum):
    Truncate = False
    Continue = True


class MaximumRetryExceeded(TimeoutError):
    """The recipient did not ack a message after every retry."""


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


def parse_number(number: bytes) -> tuple[bytes, bool, bytes | None]:
    """
    Split a message number field into ``(number, reply_ack_capable, acked)``.

    ``b"2A"`` is a plain number. ``b"2A}"`` advertises reply-ack support and
    ``b"2A}1B"`` also acks our message 1B (aprs11/replyacks.txt).
    """
    match = NUMBER_REX.match(number)
    if match is None:
        return number, False, None
    return match.group(1), match.group(2) is not None, match.group(3) or None


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


def path_split(p) -> list[Address]:
    if not p:
        return []
    if isinstance(p, str):
        return [Address.from_any(a.strip()) for a in p.split(",")]
    return [Address.from_any(a) for a in p]
