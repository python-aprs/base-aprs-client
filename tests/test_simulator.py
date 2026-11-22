import socket

import pytest

from base_aprs_client import APRSISSimulator
from base_aprs_client.simulator import SERVER_BANNER


def _connect(sim, user=None):
    s = socket.create_connection((sim.host, sim.port), timeout=5)
    f = s.makefile("rwb")
    assert f.readline() == SERVER_BANNER
    if user:
        f.write(b"user %s pass -1 vers test 0.0\r\n" % user)
        f.flush()
        assert f.readline().startswith(b"# logresp %s verified" % user)
    return s, f


@pytest.fixture
def sim():
    with APRSISSimulator() as s:
        yield s


def test_ephemeral_port(sim):
    assert sim.port != 0


def test_relay_to_others_not_sender(sim):
    s1, f1 = _connect(sim, b"N0CALL-1")
    s2, f2 = _connect(sim, b"N0CALL-2")
    sim.wait_for_clients(2)
    # login, comment and frame coalesced into one write must all be handled
    f1.write(b"# a client comment\r\nN0CALL-1>APRS:>status  \r\n")
    f1.flush()
    assert f2.readline() == b"N0CALL-1>APRS:>status  \r\n"
    assert sim.received == [(s1.getsockname(), b"N0CALL-1>APRS:>status  \r\n")]
    s1.settimeout(0.2)
    with pytest.raises(socket.timeout):
        f1.readline()
    s1.close()
    s2.close()


def test_login_and_frame_in_one_packet(sim):
    s2, f2 = _connect(sim, b"N0CALL-2")
    s1, f1 = _connect(sim)
    sim.wait_for_clients(2)
    f1.write(b"user N0CALL-1 pass -1\r\nN0CALL-1>APRS:>hi\n")
    f1.flush()
    assert f2.readline() == b"N0CALL-1>APRS:>hi\r\n"
    s1.close()
    s2.close()


def test_wait_for_clients_timeout(sim):
    with pytest.raises(TimeoutError):
        sim.wait_for_clients(1, timeout=0.05)


def test_port_in_use():
    with APRSISSimulator() as a:
        with pytest.raises(OSError):
            APRSISSimulator(port=a.port).start()
