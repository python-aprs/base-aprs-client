"""Incoming ack/rej handling, without a network."""

import asyncio
import logging

import pytest
from aprs import APRSFrame

from base_aprs_client import Client
from base_aprs_client.client import (
    MaximumRetryExceeded,
    MessageAckFuture,
    MessageRejected,
    parse_ack,
)


class FakeFrameIO:
    def __init__(self):
        self.written = []
        self.loop = asyncio.new_event_loop()

    def write(self, frame):
        self.written.append(str(frame))


@pytest.fixture
def io():
    fake = FakeFrameIO()
    yield fake
    fake.loop.close()


@pytest.fixture
def client(io):
    return Client(mycall="N0CALL", sync_frame_io=io, next_message_sequence_number=0x2A)


def receive(client, text):
    client.on_frame(APRSFrame.from_str(f"N0CALL-2>APRS::N0CALL   :{text}"))


def test_ack_resolves(client):
    future = client.send_message("N0CALL-2", "hi", ack=True)
    receive(client, "ack2a")
    assert future.result().info.text == b"ack2a"


def test_rej_raises(client):
    future = client.send_message("N0CALL-2", "hi", ack=True)
    receive(client, "rej2a")
    with pytest.raises(MessageRejected) as excinfo:
        future.result()
    assert str(excinfo.value.frame.source) == "N0CALL-2"


@pytest.mark.parametrize(
    "text",
    [
        "ack2a{5",  # a numbered message is never an ack (and gets acked itself)
        "acknowledged",
        "ack 2a",
        "ack",
        "rejoice2a",
        "ack2b",  # not outstanding
    ],
)
def test_not_an_ack(client, text):
    future = client.send_message("N0CALL-2", "hi", ack=True)
    receive(client, text)
    assert not future.done()


def test_duplicate_ack_after_rej_is_ignored(client):
    future = client.send_message("N0CALL-2", "hi", ack=True)
    receive(client, "rej2a")
    receive(client, "ack2a")  # must not raise InvalidStateError
    assert isinstance(future.exception(), MessageRejected)


def test_failed_message_is_logged(client, caplog):
    future = client.send_message("N0CALL-2", "hi", ack=True)
    receive(client, "rej2a")
    with caplog.at_level(logging.WARNING, logger="base_aprs_client.client"):
        receive(client, "unrelated")  # steps outstanding messages
    assert "Message 2a failed" in caplog.text
    assert b"2a" not in client.outstanding_messages
    assert future.done()


@pytest.mark.parametrize(
    "text, expected",
    [
        (b"ack1", (b"ack", b"1")),
        (b"rejABCDE", (b"rej", b"ABCDE")),
        (b"ackAB}", (b"ack", b"AB")),
        (b"ackAB}CD", (b"ack", b"AB")),
        (b"ackABCDEF", None),
        (b"ack", None),
        (b"Ack1", None),
        (b"ack1 ", None),
    ],
)
def test_parse_ack(text, expected):
    assert parse_ack(text) == expected


def test_zero_tries_means_zero(io):
    future = MessageAckFuture(message=None, tries=0, period=0, loop=io.loop)
    assert future.remaining_tries == 0
    future.last_try -= 1
    future.step(client=None)
    with pytest.raises(MaximumRetryExceeded):
        future.result()
