"""KISS transports: a serial TNC on a pseudo-terminal, and Dire Wolf over TCP."""

import asyncio
import os
import random
import shutil
import socket
import subprocess
import time

import pytest
from aprs import APRSFrame
from aprs.kiss import TCPKISS, SerialKISS
from ax253 import Frame
from kiss.constants import FEND

from base_aprs_client import AsyncClient, Client

pytestmark = pytest.mark.skipif(os.name != "posix", reason="needs ptys")


def kiss_encode(frame: Frame) -> bytes:
    data = bytes(frame).replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc")
    return FEND + b"\x00" + data + FEND


def kiss_frames(data: bytes) -> list[APRSFrame]:
    frames = []
    for chunk in data.split(FEND):
        if chunk[:1] == b"\x00" and len(chunk) > 1:
            raw = chunk[1:].replace(b"\xdb\xdc", b"\xc0").replace(b"\xdb\xdd", b"\xdb")
            frames.append(APRSFrame.from_bytes(raw))
    return frames


@pytest.fixture
def pty():
    """(master fd, slave device path); the client opens the slave like a TNC port."""
    master, slave = os.openpty()
    os.set_blocking(master, False)
    yield master, os.ttyname(slave)
    os.close(slave)
    os.close(master)


def read_master(master: int) -> bytes:
    try:
        return os.read(master, 4096)
    except BlockingIOError:
        return b""


async def read_frames(master: int, n: int, timeout: float = 5.0) -> list[APRSFrame]:
    data = b""
    deadline = time.monotonic() + timeout
    while len(kiss_frames(data)) < n:
        if time.monotonic() > deadline:
            raise TimeoutError(f"got {data!r}")
        data += read_master(master)
        await asyncio.sleep(0.01)
    return kiss_frames(data)


async def test_serial_tnc(pty):
    master, port = pty
    async with AsyncClient.kiss_serial("ALPHA", port, baudrate=9600) as client:
        client.send_status("over serial")
        (sent,) = await read_frames(master, 1)
        assert str(sent) == "ALPHA>APZ069:>over serial"

        # a message from the air is acked back out the serial port
        heard = APRSFrame.from_str("BRAVO>APRS::ALPHA    :hi on rf{7")
        os.write(master, kiss_encode(heard))
        frame = await client.recv(timeout=5)
        assert frame.info.text == b"hi on rf"
        (ack,) = await read_frames(master, 1)
        assert str(ack) == "ALPHA>APZ069::BRAVO    :ack7"


def test_serial_tnc_sync(pty):
    """The legacy wrapper around an aprs.SerialKISS transport."""
    master, port = pty
    transport = SerialKISS(port, "9600")
    transport.start_no_config()
    client = Client("ALPHA", transport)
    try:
        client.send_status("legacy serial")
        client.poll(0.1)
        frames = kiss_frames(read_master(master))
        assert [str(f) for f in frames] == ["ALPHA>APZ069:>legacy serial"]
        os.write(master, kiss_encode(APRSFrame.from_str("BRAVO>APRS:>hello")))
        assert client.read(min_frames=1)[0].info.raw == b">hello"
    finally:
        client.close()
        transport.stop()


# --- Dire Wolf ------------------------------------------------------------------

DIREWOLF = shutil.which("direwolf")
# direwolf block-buffers its output to a file; ask for line buffering
STDBUF = ["stdbuf", "-oL"] if shutil.which("stdbuf") else []


def free_port() -> int:
    """A free port in the range Dire Wolf accepts (1024 to 49151)."""
    for _ in range(100):
        port = random.randint(20000, 49151)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port")


@pytest.fixture
def direwolf(tmp_path):
    """Dire Wolf with no sound card: frames sent to it are logged, then discarded."""
    if DIREWOLF is None:
        pytest.skip("direwolf is not installed")
    port = free_port()
    conf = tmp_path / "direwolf.conf"
    conf.write_text(
        "ADEVICE null null\nCHANNEL 0\nMYCALL N0CALL\nMODEM 1200\n"
        f"KISSPORT {port}\nAGWPORT 0\n"
    )
    log = tmp_path / "direwolf.log"
    with log.open("wb") as out:
        proc = subprocess.Popen(
            [*STDBUF, DIREWOLF, "-t", "0", "-c", str(conf)],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=tmp_path,
        )
    try:
        wait_for_log(log, f"port {port}")
        yield port, log
    finally:
        proc.terminate()
        proc.wait(5)


def wait_for_log(log, text: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while text not in (content := log.read_text(errors="replace")):
        if time.monotonic() > deadline:
            raise TimeoutError(f"{text!r} not in direwolf output:\n{content}")
        time.sleep(0.05)
    return content


async def test_direwolf_transmits(direwolf):
    port, log = direwolf
    async with AsyncClient.kiss_tcp("ALPHA", "127.0.0.1", port) as client:
        client.send_status("via dire wolf")
        await asyncio.to_thread(wait_for_log, log, "[0L] ALPHA>APZ069:>via dire wolf")


def test_direwolf_sync(direwolf):
    """The legacy wrapper around an aprs.TCPKISS transport."""
    port, log = direwolf
    transport = TCPKISS("127.0.0.1", port)
    transport.start()
    client = Client("ALPHA", transport)
    try:
        client.send_message("BRAVO", "legacy kiss")
        client.poll(0.1)
        wait_for_log(log, "[0L] ALPHA>APZ069::BRAVO    :legacy kiss")
    finally:
        client.close()
        transport.stop()
