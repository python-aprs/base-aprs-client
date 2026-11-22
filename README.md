# base-aprs-client

Base layer for building APRS client applications in Python, on top of
[aprs3](https://github.com/python-aprs/aprs3),
[kiss3](https://github.com/python-aprs/kiss3) and
[ax253](https://github.com/python-aprs/ax253).

`AsyncClient` (asyncio) and `Client` (its synchronous wrapper) implement
the APRS station semantics that every application needs:

* sending messages, with acknowledgement and timed retries
* acknowledging incoming messages addressed to `mycall`, and delivering
  each one once even when the sender retries
* [reply-acks](http://www.aprs.org/aprs11/replyacks.txt), on by default
* splitting long messages on word boundaries, or truncating them
* sending status and position reports
* reconnecting with backoff when the connection drops, keeping
  unacknowledged messages
* with a `PacketStore`, logging every frame and sending queued frames when
  they are due, even across restarts

It works over APRS-IS, KISS-over-TCP (e.g. Dire Wolf) or a serial KISS TNC.

## Example

```python
import asyncio
from base_aprs_client import AsyncClient


async def main():
    async with AsyncClient.aprsis(
        "N0CALL-1",
        passcode="-1",  # use a real passcode to transmit
        filter="r/46.1/-122.9/50",
    ) as client:
        client.send_status("Hello from base-aprs-client")
        await client.send_message("N0CALL-2", "hi", ack=True)  # waits for the ack
        async for frame in client:
            print(frame)


asyncio.run(main())
```

The synchronous `Client` runs the same client on a background thread:

```python
from base_aprs_client import Client

with Client.kiss_tcp("N0CALL-1", "localhost", 8001) as client:
    ack = client.send_message("N0CALL-2", "hi", ack=True)
    print(ack.result(timeout=60))
    for frame in client.read(min_frames=1):
        print(frame)
```

`Client(mycall, sync_frame_io)` with an already started `aprs.TCP`,
`aprs.TCPKISS` or `aprs.SerialKISS` still works as before; its event loop
only runs inside the client's calls.

## Local testing without a radio

`APRSISSimulator` is a tiny APRS-IS-like relay: every line one client sends
is forwarded to all the others.

```python
from aprs import TCP
from base_aprs_client import APRSISSimulator, Client

with APRSISSimulator() as sim:
    with TCP(host=sim.host, port=sim.port) as t1, TCP(host=sim.host, port=sim.port) as t2:
        sim.wait_for_clients(2)
        alice = Client("N0CALL-1", t1)
        bob = Client("N0CALL-2", t2)
        alice.send_message(bob.mycall, "hi bob")
        print(bob.read(min_frames=1))
```

Run it standalone on port 14588 with `python -m base_aprs_client.simulator`.

## Packet log and send queue

`base_aprs_client.store.PacketStore` keeps a log of frames heard and sent,
and a queue of frames to transmit later. It uses SQLAlchemy 2.0 with an
async session, so it never blocks the event loop; SQLite through aiosqlite
is the default, and any async SQLAlchemy URL works. Install with
`pip install base-aprs-client[db]`.

Give it to a client to log everything it hears and sends, and to transmit
frames scheduled with `client.schedule(frame, delay=...)`:

```python
from aprs import InformationField
from base_aprs_client import AsyncClient
from base_aprs_client.store import PacketStore

async with PacketStore("sqlite+aiosqlite:///N0CALL.db") as store:
    async with AsyncClient.aprsis("N0CALL-1", passcode="12345", store=store) as client:
        await client.schedule(InformationField.from_bytes(b">QRT soon"), delay=600)
```

The store can also be used on its own:

```python
from base_aprs_client.store import PacketStore

async with PacketStore("sqlite+aiosqlite:///N0CALL.db") as store:
    await store.record_received(frame)
    await store.schedule(reply, delay=10)
    for record in await store.due():
        if await store.claim(record):  # False if another task already sent it
            transport.write(record.frame)
```

## Development

```
uv sync --all-extras
uv run pytest
```

CI runs the suite on every supported Python (3.11+). The KISS tests run a
serial TNC on a pseudo-terminal, and send through
[Dire Wolf](https://github.com/wb2osz/direwolf) when it is installed
(`apt install direwolf`); they are skipped otherwise. Lint and format with
`uv run ruff check` and `uv run ruff format`, type-check with `uv run ty check`.
