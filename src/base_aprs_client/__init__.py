"""APRS station behaviour for applications.

``AsyncClient`` sends messages with acknowledgement and timer-driven retry,
acks incoming messages addressed to ``mycall`` (once each, with reply-ack
support), sends status and position reports, reconnects when the transport
drops, and can log frames and run a delayed send queue with ``PacketStore``.
``Client`` is its synchronous wrapper. ``APRSISSimulator`` is a local
APRS-IS-like relay for testing without a radio.

Two stations talking through the simulator. Each ``Client`` runs on a
background thread, so N0CALL-2 acks the message without being asked:

>>> from base_aprs_client import APRSISSimulator, Client
>>> with APRSISSimulator() as sim:
...     with (
...         Client.aprsis("N0CALL-1", sim.host, sim.port) as alice,
...         Client.aprsis("N0CALL-2", sim.host, sim.port) as bob,
...     ):
...         sim.wait_for_clients(2)
...         ack = alice.send_message("N0CALL-2", "see you at the net", ack=True)
...         print("acked by", ack.result(timeout=10).source)
...         (frame,) = bob.read(min_frames=1)
...         print(frame.source, "says", frame.info.text.decode())
acked by N0CALL-2
N0CALL-1 says see you at the net

``Client.aprsis``, ``Client.kiss_tcp`` and ``Client.kiss_serial`` (and the
same constructors on ``AsyncClient``) connect to APRS-IS or a KISS TNC.
``connect`` takes a URL instead: ``aprsis://rotate.aprs2.net?passcode=12345``,
``kiss+tcp://localhost:8001`` or ``kiss+serial:///dev/ttyUSB0?baud=9600``.

A bot answers each message with ``messages()``, which leaves out acks and
duplicates, and ``reply()``::

    async with AsyncClient.connect("N0CALL-1", "kiss+tcp://localhost:8001") as client:
        async for frame in client.messages():
            await client.reply(frame, "pong", ack=True)
"""

from importlib.metadata import version

from .aio import AsyncClient, NotConnected, ReconnectPolicy
from .client import Client, MessageAckFuture
from .common import MaximumRetryExceeded, MessageRejected, OverflowDisposition
from .simulator import APRSISSimulator

__author__ = "Masen Furer KF7HVM <kf7hvm@0x26.net>"
__copyright__ = "Copyright 2022 Masen Furer and Contributors"
__license__ = "Apache License, Version 2.0"
__distribution__ = "base-aprs-client"
__version__ = version(__distribution__)
__all__ = [
    "APRSISSimulator",
    "AsyncClient",
    "Client",
    "MaximumRetryExceeded",
    "MessageAckFuture",
    "MessageRejected",
    "NotConnected",
    "OverflowDisposition",
    "ReconnectPolicy",
    "__distribution__",
    "__version__",
]
