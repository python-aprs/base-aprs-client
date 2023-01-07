import pytest

from aprs import Position

from base_aprs_client.client import OverflowDisposition


@pytest.mark.parametrize(
    "overflow", [OverflowDisposition.Truncate, OverflowDisposition.Continue]
)
def test_message(client1, client2, overflow):
    client1.send_message(client2.mycall, "This is the message", overflow=overflow)
    client2.read(min_frames=1)
    assert client2.messages[0].text == b"This is the message"


@pytest.mark.parametrize("ack", [True, False])
def test_message_overflow(client1, client2, ack):
    zen = (
        "Beautiful is better than ugly. "
        "Explicit is better than implicit. "
        "Simple is better than complex. "
        "Complex is better than complicated. "
        "Flat is better than nested. "
        "Sparse is better than dense. "
        "Readability counts. "
    )
    client1.send_message(
        client2.mycall, zen, ack=ack, overflow=OverflowDisposition.Continue
    )
    client2.read(min_frames=4)
    assert len(client2.messages) == 4
    if ack:
        messages = sorted(client2.messages, key=lambda m: m.number)
    else:
        messages = client2.messages
    reconstructed = b"".join(m.text for m in messages)
    assert reconstructed.decode() == zen


def test_message_ack(client1, client2):
    next_seq = client1.next_message_sequence_number
    maf = client1.send_message(client2.mycall, "ack me back", ack=True)
    client2.read(min_frames=1)
    client1.read(min_frames=1)
    assert maf.result().info.text == b"ack%x" % next_seq
    assert client2.messages[0].text == b"ack me back"
    assert client2.messages[0].number == b"%x" % next_seq
    assert client1.messages[0].text == b"ack%x" % next_seq
    assert client1.messages[0].number is None


def test_status(client1, client2):
    client1.send_status("foo")
    client2.read(min_frames=1)
    assert client2.frames[0].info.raw == b">foo"


def test_position(client1, client2):
    pos = Position()
    sent_frame = client1.send_position(position=pos)
    assert sent_frame.info._position == pos
    client2.read(min_frames=1)
    assert client2.frames[0].info._position == pos
