"""The net control logic behind examples/aprs_net, against the simulator."""

import asyncio
import csv
import io
import pathlib
import sys
import time

import pytest
from aprs import APRSFrame

from base_aprs_client import APRSISSimulator, AsyncClient

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "examples" / "aprs_net"))

from aprs_net.net import CALLSIGN, Net, Settings, base_call  # noqa: E402


async def eventually(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.01)


@pytest.fixture
def sim():
    with APRSISSimulator() as s:
        yield s


def settings(sim, **kwargs) -> Settings:
    return Settings(
        mycall="NETCTL", address=f"{sim.host}:{sim.port}", net_name="Test Net", **kwargs
    )


def station(sim, call):
    return AsyncClient.aprsis(call, sim.host, sim.port)


@pytest.fixture
async def net(sim):
    n = Net(settings(sim))
    await n.open()
    yield n
    await n.close(summary=False)


async def test_checkin_by_message(sim, net):
    async with station(sim, "KF7HVM-9") as mobile:
        replies = []
        mobile.on_message(
            lambda m, f: m.text.startswith(b"ack") or replies.append(m.text)
        )
        await eventually(lambda: sim.n_clients == 2)
        await asyncio.wait_for(
            mobile.send_message("NETCTL", "checking in with n7dem", ack=True), 5
        )
        await eventually(lambda: replies)
        assert list(net.checkins) == ["KF7HVM", "N7DEM"]
        assert net.checkins["N7DEM"].heard_from == "KF7HVM-9"
        assert replies == [b"ACK KF7HVM N7DEM, thanks for checking in to Test Net"]
        # the reply is acked by the station's client
        await eventually(lambda: net.sent[0].status == "acked")

        # checking in again doesn't get another reply
        await asyncio.wait_for(mobile.send_message("NETCTL", "still here", ack=True), 5)
        await asyncio.sleep(0.1)
        assert len(net.sent) == 1
        assert net.checkins["KF7HVM"].text == "checking in with n7dem"


async def test_positions_and_csv(sim, net):
    async with station(sim, "W1AW-7") as w1aw:
        await eventually(lambda: sim.n_clients == 2)
        w1aw.write(APRSFrame.from_str("W1AW-7>APRS:!4144.37N/07243.65W>"))
        await eventually(lambda: "W1AW" in net.positions)
    assert net.add_checkin("w1aw-7", "voice")
    assert not net.add_checkin("W1AW"), "already checked in"
    rows = list(csv.DictReader(io.StringIO(net.checkins_csv())))
    assert rows[0]["callsign"] == "W1AW"
    assert rows[0]["message"] == "voice"
    assert float(rows[0]["lat"]) == pytest.approx(41.7395)
    net.remove_checkin("W1AW")
    assert net.checkins == {}


async def test_acks_are_not_checkins(sim, net):
    async with station(sim, "N7DEM") as n7dem:
        await eventually(lambda: sim.n_clients == 2)
        net.send_message("N7DEM", "are you on the net?")
        await eventually(lambda: net.sent[0].status != "sending")
    assert net.sent[0].status == "acked"
    assert net.checkins == {}
    assert n7dem.outstanding == {}


async def test_timed_status_and_closing_summary(sim):
    async with station(sim, "N7DEM") as listener:
        await eventually(lambda: sim.n_clients == 1)
        heard = []
        listener.on_frame(lambda f: heard.append(str(f)))
        net = Net(settings(sim, status_text="Net control here", status_minutes=10))
        await net.open()
        await eventually(lambda: heard)
        assert heard == ["NETCTL>APZ069:>Net control here"]
        net.add_checkin("N7DEM")
        await net.close()
        await eventually(lambda: len(heard) == 2)
    assert heard[1] == "NETCTL>APZ069:>Test Net closed with 1 check-in"
    assert not net.running
    assert [h.direction for h in net.heard] == ["tx", "tx"]


def test_callsigns_and_base_call():
    text = "CHECKING IN WITH N7DEM AND 2E0ABC, NOT 146.52 OR WIDE1-1"
    assert CALLSIGN.findall(text) == ["N7DEM", "2E0ABC"]
    assert base_call("kf7hvm-9") == "KF7HVM"


def test_settings_build_each_connection():
    for connection, address in [
        ("aprsis", "localhost:14580"),
        ("kiss_tcp", "localhost:8001"),
        ("kiss_serial", "/dev/ttyUSB0"),
    ]:
        client = Settings(connection=connection, address=address).client()
        assert str(client.mycall) == "N0CALL"
    with pytest.raises(ValueError, match="unknown connection"):
        Settings(connection="carrier pigeon").client()
