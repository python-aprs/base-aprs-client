"""The scripts in examples/ work against the APRS-IS simulator."""

import asyncio
import pathlib
import sys
import threading

import pytest
from aprs import Message

from base_aprs_client import AsyncClient, Client

EXAMPLES = pathlib.Path(__file__).parent.parent / "examples"
sys.path.insert(0, str(EXAMPLES))

import chat  # noqa: E402
import send_message  # noqa: E402


def aprsis(server) -> str:
    return f"{server.host}:{server.port}"


def test_send_message_acked(server, capsys):
    with Client.aprsis("N0CALL-2", server.host, server.port, reconnect=None) as peer:
        server.wait_for_clients(1)
        argv = ["--call", "N0CALL-1", "--to", "N0CALL-2", "--aprsis", aprsis(server)]
        assert send_message.main([*argv, "see you at the net"]) == 0
        (frame,) = peer.read(min_frames=1)
    assert isinstance(frame.info, Message)
    assert frame.info.text == b"see you at the net"
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("sent: N0CALL-1>APZ069::N0CALL-2 :see you at the net{")
    assert out[1] == "acked by N0CALL-2"


def test_send_message_long_text_is_split(server, capsys):
    with Client.aprsis("N0CALL-2", server.host, server.port, reconnect=None) as peer:
        server.wait_for_clients(1)
        argv = ["--call", "N0CALL-1", "--to", "N0CALL-2", "--aprsis", aprsis(server)]
        assert send_message.main([*argv, "word " * 20]) == 0
        frames = peer.read(min_frames=2)
    assert len(frames) == 2
    assert capsys.readouterr().out.count("acked by N0CALL-2") == 2


def test_send_message_without_ack_fails(server, capsys):
    argv = ["--call", "N0CALL-1", "--to", "N0CALL-9", "--aprsis", aprsis(server)]
    argv += ["--retries", "1", "--retry-period", "0.05", "hello?"]
    assert send_message.main(argv) == 1
    assert "no ack after 1 retries" in capsys.readouterr().out


def test_connection_args():
    parser = send_message.argparse.ArgumentParser()
    send_message.add_connection_args(parser)
    call = ["--call", "N0CALL-1"]
    args = parser.parse_args([*call, "--tcp", "localhost:8001", "--path", "WIDE2-1"])
    assert send_message.connection(args) == (
        "kiss_tcp",
        ("N0CALL-1", "localhost", 8001),
        {"default_path": "WIDE2-1"},
    )
    args = parser.parse_args([*call, "--serial", "/dev/ttyUSB0", "--baud", "1200"])
    assert send_message.connection(args)[:2] == (
        "kiss_serial",
        ("N0CALL-1", "/dev/ttyUSB0", 1200),
    )
    args = parser.parse_args([*call, "--aprsis", "example.net", "--passcode", "1"])
    assert send_message.connection(args) == (
        "aprsis",
        ("N0CALL-1", "example.net", 14580, "1"),
        {},
    )


async def test_chat(server, capsys):
    replied = threading.Event()

    async def lines():
        yield "hello\n"
        await asyncio.to_thread(replied.wait, 5)
        yield "/to n0call-3\n"
        yield "anyone there?\n"

    async with (
        AsyncClient.aprsis("N0CALL-1", server.host, server.port, retry_period=0.1) as me,
        AsyncClient.aprsis("N0CALL-2", server.host, server.port) as peer,
    ):
        me.retries = 1
        await asyncio.to_thread(server.wait_for_clients, 2)

        @peer.on_message
        def reply(message, frame):
            peer.submit_message(str(frame.source), "hi there")
            replied.set()

        await asyncio.wait_for(chat.chat(me, "N0CALL-2", lines()), 5)
    out = capsys.readouterr().out.splitlines()
    assert "  ✓ N0CALL-2 got it" in out
    assert "N0CALL-2: hi there" in out
    assert "  now talking to N0CALL-3" in out
    assert out[-1] == "  ✗ no ack from N0CALL-3"


@pytest.mark.parametrize("module", [chat, send_message])
def test_docstring_mentions_simulator(module):
    assert "python -m base_aprs_client.simulator" in (module.__doc__ or "")
