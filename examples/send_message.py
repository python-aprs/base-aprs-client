#!/usr/bin/env python3
"""
Send one APRS message and wait for the recipient's ack.

Works over APRS-IS, a KISS TNC on TCP (like Dire Wolf), or a serial TNC::

    $ python send_message.py --call N0CALL-1 --to N0CALL-2 "see you at the net" \\
        --aprsis rotate.aprs2.net:14580 --passcode 12345
    sent: N0CALL-1>APZ069::N0CALL-2 :see you at the net{2a}
    acked by N0CALL-2

    $ python send_message.py --call N0CALL-1 --to N0CALL-2 "hi" --tcp localhost:8001

Messages longer than 67 characters are split into parts, and each part is
retried until it is acked or ``--retries`` run out. Exits with status 1 if
any part wasn't acked. Uses the synchronous ``Client``; see ``chat.py`` for
``AsyncClient``.

To try it without a radio, run ``python -m base_aprs_client.simulator`` and
use ``--aprsis localhost:14588``, with ``chat.py`` as the other station.
"""

import argparse
import sys

from base_aprs_client import Client, MaximumRetryExceeded, MessageRejected
from base_aprs_client.common import OverflowDisposition


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    """--aprsis / --tcp / --serial, shared with chat.py."""
    parser.add_argument("--call", required=True, help="your callsign, e.g. N0CALL-1")
    via = parser.add_mutually_exclusive_group(required=True)
    via.add_argument("--aprsis", metavar="HOST:PORT", help="APRS-IS server")
    via.add_argument("--tcp", metavar="HOST:PORT", help="KISS TNC over TCP")
    via.add_argument("--serial", metavar="DEVICE", help="KISS TNC on a serial port")
    parser.add_argument("--passcode", default="-1", help="APRS-IS passcode")
    parser.add_argument("--baud", type=int, default=9600, help="serial speed")
    parser.add_argument("--path", default="WIDE1-1", help="digipeater path for RF")


def connection(args: argparse.Namespace) -> tuple[str, tuple, dict]:
    """The constructor name, arguments and options for the chosen connection."""
    if args.aprsis:
        host, _, port = args.aprsis.partition(":")
        # APRS-IS servers don't digipeat, so no path
        return "aprsis", (args.call, host, int(port or 14580), args.passcode), {}
    options = {"default_path": args.path}
    if args.tcp:
        host, _, port = args.tcp.partition(":")
        return "kiss_tcp", (args.call, host, int(port or 8001)), options
    return "kiss_serial", (args.call, args.serial, args.baud), options


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_connection_args(parser)
    parser.add_argument("--to", required=True, help="recipient callsign")
    parser.add_argument("text", help="the message")
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-period", type=float, default=30, help="seconds")
    args = parser.parse_args(argv)

    name, conn_args, options = connection(args)
    options.update(retries=args.retries, retry_period=args.retry_period)
    with getattr(Client, name)(*conn_args, **options) as client:
        result = client.send_message(
            args.to, args.text, ack=True, overflow=OverflowDisposition.Continue
        )
        acks = result if isinstance(result, list) else [result]
        failed = False
        for ack in acks:
            assert ack is not None  # always a future with ack=True
            print(f"sent: {client.prepare_frame(ack.message)}", flush=True)
            try:
                frame = ack.result()
                print(f"acked by {frame.source}", flush=True)
            except MessageRejected as exc:
                print(f"rejected: {exc}", flush=True)
                failed = True
            except MaximumRetryExceeded:
                print(f"no ack after {args.retries} retries", flush=True)
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
