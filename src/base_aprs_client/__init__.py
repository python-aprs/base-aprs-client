from importlib_metadata import version

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
