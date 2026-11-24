import threading

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
    (maf,) = client1.send_message(client2.mycall, "ack me back", ack=True)
    client2.read(min_frames=1)
    client1.read(min_frames=1)
    # reply-ack capable: the number ends in "}" and so does the ack
    ack = maf.result()
    assert ack is not None and ack.info.text == b"ack%x}" % next_seq
    assert client2.messages[0].text == b"ack me back"
    assert client2.messages[0].number == b"%x}" % next_seq
    # base-aprs-client#27: the ack reaches on_frame, not on_message
    assert client1.frames[0].info.text == b"ack%x}" % next_seq
    assert client1.messages == []


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


def test_send_from_another_thread_while_reading(client1, client2):
    """A worker thread can send while the main thread waits in read()."""
    timer = threading.Timer(0.2, client1.send_status, args=("from a thread",))
    timer.start()
    frames = client2.read(min_frames=1)
    timer.join()
    assert frames[0].info.raw == b">from a thread"


def test_send_from_another_thread_while_idle(client1, client2):
    worker = threading.Thread(target=client1.send_status, args=("idle main",))
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert client2.read(min_frames=1)[0].info.raw == b">idle main"


def test_ack_wait_from_another_thread(client1, client2):
    (maf,) = client1.send_message(client2.mycall, "ack me", ack=True)
    result = []
    worker = threading.Thread(target=lambda: result.append(maf.result(timeout=5)))
    worker.start()
    client2.read(min_frames=1)  # client2 hears it and acks
    worker.join(5)
    assert result[0].info.text.startswith(b"ack")


def test_client_call_from_loop_is_refused(client1, client2):
    """A sync call from a handler running on the loop would deadlock; it raises."""
    errors = []

    def handler(frame):
        try:
            client1.send_status("inside")
        except RuntimeError as exc:
            errors.append(exc)

    client1.aclient.on_frame(handler)
    client2.send_status("trigger")
    client1.read(min_frames=1)
    assert errors


# --- breaking changes from the API review ---


def test_send_message_always_returns_a_list(client1, client2):
    """#28: one part or many, with or without ack."""
    import concurrent.futures

    (plain,) = client1.send_message(client2.mycall, "no ack")
    assert isinstance(plain, concurrent.futures.Future)
    assert plain.done() and plain.result() is None
    parts = client1.send_message(
        client2.mycall, "word " * 20, ack=True, overflow=OverflowDisposition.Continue
    )
    assert len(parts) == 2
    client2.read(min_frames=3)
    client1.read(min_frames=2)
    done, not_done = concurrent.futures.wait(parts, timeout=5)
    assert not not_done
    assert all(f.result().info.text.startswith(b"ack") for f in done)


def test_message_ack_future_cancel(client1, client2):
    (future,) = client1.send_message(client2.mycall, "never acked", ack=True)
    seen = []
    future.add_done_callback(seen.append)
    assert future.cancel()
    assert future.cancelled() and seen == [future]
    assert not future.cancel()  # already done


def test_constructor_does_not_connect():
    """#29: nothing touches the network until start(), with, or a send."""
    import socket

    from base_aprs_client import Client

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # nothing listens here now
    client = Client.aprsis("N0CALL", "127.0.0.1", port, reconnect=None)
    assert not client.connected
    with pytest.raises(OSError):
        client.start()
    client.close()


def test_send_starts_the_client(server):
    from base_aprs_client import Client

    a = Client.aprsis("N0CALL-5", server.host, server.port, reconnect=None)
    b = Client.aprsis("N0CALL-6", server.host, server.port, reconnect=None)
    try:
        assert not a.connected
        b.start()
        a.send_status("lazy")  # starts a
        assert a.connected
        server.wait_for_clients(2)
        a.send_status("lazy 2")
        frames = b.read(min_frames=1)
        assert frames[-1].info.raw.startswith(b">lazy")
    finally:
        a.close()
        b.close()
