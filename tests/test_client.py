import time

import attrs
import pytest

from aprs import APRSFrame, Message, TCP
from base_aprs_client import Client


@attrs.define
class MessageSavingClient(Client):
    client2_rxd = attrs.field(factory=list)

    def on_message(self, message: Message, frame: APRSFrame) -> None:
        super().on_message(message, frame)
        self.client2_rxd.append(frame)
    

@pytest.fixture
def client2(server):
    with TCP(
        host="127.0.0.1",
        port=server.port,
    ) as sync_frame_io:
        yield MessageSavingClient(
            mycall="CLIENT2",
            sync_frame_io=sync_frame_io,
        )


def test_message(client1, client2):
    print("connect now")
    time.sleep(5)
    client1.send_message(client2.mycall, "This is the message")
    client2.read(min_frames=1)
    assert client2.client2_rxd