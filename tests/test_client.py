from aprs import Position


def test_message(client1, client2):
    client1.send_message(client2.mycall, "This is the message")
    client2.read(min_frames=1)
    assert client2.messages[0].text == b"This is the message"


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
