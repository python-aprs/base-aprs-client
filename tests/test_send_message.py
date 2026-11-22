"""send_message framing, without a network: frames are captured as written."""

import asyncio

import pytest

from base_aprs_client import Client
from base_aprs_client.client import OverflowDisposition, split_message, truncate_message


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
    return Client(
        mycall="N0CALL",
        sync_frame_io=io,
        default_path="WIDE1-1",
        next_message_sequence_number=0x10,
    )


def test_truncate_is_the_default(client, io):
    client.send_message("N0CALL-2", "x" * 80)
    assert io.written == ["N0CALL>APZ069,WIDE1-1::N0CALL-2 :" + "x" * 67]


def test_truncate_with_ack_keeps_the_number(client, io):
    client.send_message("N0CALL-2", "x" * 80, ack=True)
    assert io.written == ["N0CALL>APZ069,WIDE1-1::N0CALL-2 :" + "x" * 67 + "{10"]


def test_truncate_does_not_split_utf8(client, io):
    client.send_message("N0CALL-2", "x" * 66 + "é")  # 68 bytes
    (frame,) = io.written
    assert frame.endswith(":" + "x" * 66)


def test_continue_uses_requested_path(client, io):
    futures = client.send_message(
        "N0CALL-2",
        "y" * 80,
        overflow=OverflowDisposition.Continue,
        path=["WIDE2-1"],
    )
    assert futures == [None, None]
    assert io.written == [
        "N0CALL>APZ069,WIDE2-1::N0CALL-2 :" + "y" * 67,
        "N0CALL>APZ069,WIDE2-1::N0CALL-2 :" + "y" * 13,
    ]


def test_continue_with_ack_numbers_each_part(client, io):
    futures = client.send_message(
        "N0CALL-2", "z" * 80, ack=True, overflow=OverflowDisposition.Continue
    )
    assert [f.message.number for f in futures] == [b"10", b"11"]
    assert [frame[-3:] for frame in io.written] == ["{10", "{11"]
    assert client.next_message_sequence_number == 0x12


def test_continue_short_message_is_not_a_list(client, io):
    future = client.send_message(
        "N0CALL-2", "short", ack=True, overflow=OverflowDisposition.Continue
    )
    assert future is client.outstanding_messages[b"10"]


@pytest.mark.parametrize(
    "text, chunks",
    [
        ("", [""]),
        ("hello", ["hello"]),
        ("a" * 67, ["a" * 67]),
        ("a" * 68, ["a" * 67, "a"]),
        # breaks after the last space that fits, keeping words whole
        ("word " * 14, ["word " * 13, "word "]),
        ("a" * 60 + " " + "b" * 10, ["a" * 60 + " ", "b" * 10]),
        # a space at index 0 doesn't count as a break
        (" " + "c" * 70, [" " + "c" * 66, "c" * 4]),
        ("é" * 40, ["é" * 33, "é" * 7]),
    ],
)
def test_split_message(text, chunks):
    assert split_message(text) == chunks
    assert "".join(chunks) == text
    assert all(len(c.encode()) <= 67 for c in chunks)


def test_truncate_message():
    assert truncate_message("a" * 70) == "a" * 67
    assert truncate_message("short") == "short"
    assert truncate_message("é" * 40) == "é" * 33
