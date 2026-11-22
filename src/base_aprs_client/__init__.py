"""APRS station behaviour for applications.

``Client`` sends messages with optional acknowledgement and retry, acks
incoming messages addressed to ``mycall``, and sends status and position
reports over any ``aprs`` transport. ``APRSISSimulator`` is a local
APRS-IS-like relay for testing without a radio.
"""

from importlib.metadata import version

from .client import Client
from .simulator import APRSISSimulator

__author__ = "Masen Furer KF7HVM <kf7hvm@0x26.net>"
__copyright__ = "Copyright 2022 Masen Furer and Contributors"
__license__ = "Apache License, Version 2.0"
__distribution__ = "base-aprs-client"
__version__ = version(__distribution__)
__all__ = [
    "APRSISSimulator",
    "Client",
    "__distribution__",
    "__version__",
]
