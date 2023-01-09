import attrs
import pytest

from aprs import TCP
from base_aprs_client import APRSISSimulator, Client


@pytest.fixture
def server():
    with APRSISSimulator() as s:
        yield s


@attrs.define
class FrameSavingClient(Client):
    frames = attrs.field(factory=list)
    messages = attrs.field(factory=list)

    def on_frame(self, frame):
        super().on_frame(frame)
        self.frames.append(frame)

    def on_message(self, message, frame):
        super().on_message(message, frame)
        self.messages.append(message)


@pytest.fixture
def client1(server):
    with TCP(
        host="127.0.0.1",
        port=server.port,
    ) as sync_frame_io:
        yield FrameSavingClient(
            mycall="CLIENT1",
            sync_frame_io=sync_frame_io,
        )


@pytest.fixture
def client2(server):
    with TCP(
        host="127.0.0.1",
        port=server.port,
    ) as sync_frame_io:
        yield FrameSavingClient(
            mycall="CLIENT2",
            sync_frame_io=sync_frame_io,
        )
