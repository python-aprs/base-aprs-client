"""APRS station behaviour for applications.

``AsyncClient`` sends messages with acknowledgement and timer-driven retry,
acks incoming messages addressed to ``mycall`` (once each, with reply-ack
support), sends status and position reports, reconnects when the transport
drops, and can log frames and run a delayed send queue with ``PacketStore``.
``Client`` is its synchronous wrapper. ``APRSISSimulator`` is a local
APRS-IS-like relay for testing without a radio.
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
