#!/usr/bin/env python3
"""
Chat with another station by APRS message.

Each line you type is sent to ``--to`` and marked when it's acked. Messages
addressed to you are printed as they arrive, from any station::

    $ python chat.py --call N0CALL-1 --to N0CALL-2 --aprsis localhost:14588
    hello
      ✓ N0CALL-2 got it
    N0CALL-2: hi there

Type ``/to CALL`` to talk to a different station, and Ctrl-D to quit.

To try it without a radio, start the local relay with
``python -m base_aprs_client.simulator`` and run two copies with
``--aprsis localhost:14588`` and swapped callsigns. On the air, use
``--aprsis rotate.aprs2.net:14580 --passcode N``, ``--tcp localhost:8001`` for
Dire Wolf, or ``--serial /dev/ttyUSB0``. Uses ``AsyncClient``; see
``send_message.py`` for the synchronous ``Client``.
"""

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator

from aprs import APRSFrame, Message
from send_message import add_connection_args, connection

from base_aprs_client import AsyncClient, MaximumRetryExceeded, MessageRejected
from base_aprs_client.common import OverflowDisposition, parse_ack


async def wait_for_ack(future: asyncio.Future, peer: str) -> None:
    try:
        await future
        print(f"  ✓ {peer} got it", flush=True)
    except MessageRejected:
        print(f"  ✗ {peer} rejected it", flush=True)
    except MaximumRetryExceeded:
        print(f"  ✗ no ack from {peer}", flush=True)


async def chat(client: AsyncClient, peer: str, lines: AsyncIterator[str]) -> None:
    """Send each line to ``peer``; print incoming messages until lines run out."""

    @client.on_message
    def show(message: Message, frame: APRSFrame) -> None:
        if message.number is None and parse_ack(message.text):
            return  # a bare ack or reject; wait_for_ack reports those
        print(f"{frame.source}: {message.text.decode('utf-8', 'replace')}", flush=True)

    pending = set()
    async for line in lines:
        line = line.strip()
        if line.startswith("/to "):
            peer = line[4:].strip().upper()
            print(f"  now talking to {peer}", flush=True)
        elif line:
            futures = client.submit_message(
                peer, line, ack=True, overflow=OverflowDisposition.Continue
            )
            pending.update(asyncio.ensure_future(wait_for_ack(f, peer)) for f in futures)
    if pending:  # let the last messages get their acks before quitting
        await asyncio.wait(pending)


async def stdin_lines() -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        yield line.decode()


async def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_connection_args(parser)
    parser.add_argument("--to", required=True, help="station to talk to")
    args = parser.parse_args(argv)
    name, conn_args, options = connection(args)
    async with getattr(AsyncClient, name)(*conn_args, **options) as client:
        await chat(client, args.to.upper(), stdin_lines())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
