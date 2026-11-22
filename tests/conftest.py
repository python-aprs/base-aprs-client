import dataclasses

import pytest
from aprs import TCP

from base_aprs_client import APRSISSimulator, Client


@pytest.fixture
def server():
    with APRSISSimulator() as s:
        yield s


@dataclasses.dataclass(slots=True)
class FrameSavingClient(Client):
    frames: list = dataclasses.field(default_factory=list)
    messages: list = dataclasses.field(default_factory=list)

    def on_frame(self, frame):
        super(FrameSavingClient, self).on_frame(frame)
        self.frames.append(frame)

    def on_message(self, message, frame):
        super(FrameSavingClient, self).on_message(message, frame)
        self.messages.append(message)


def _client(server, mycall):
    sync_frame_io = TCP(host=server.host, port=server.port, user=mycall)
    sync_frame_io.start()
    return FrameSavingClient(mycall=mycall, sync_frame_io=sync_frame_io)


@pytest.fixture
def client1(server):
    client = _client(server, "CLIENT1")
    yield client
    client.sync_frame_io.stop()


@pytest.fixture
def client2(server, client1):
    client = _client(server, "CLIENT2")
    server.wait_for_clients(2)
    yield client
    client.sync_frame_io.stop()
