# base-aprs-client

Base layer for building APRS client applications in Python, on top of
[aprs3](https://github.com/python-aprs/aprs3),
[kiss3](https://github.com/python-aprs/kiss3) and
[ax253](https://github.com/python-aprs/ax253).

`Client` implements the APRS station semantics that every application
needs:

* sending messages, with optional acknowledgement and retry
* automatically acknowledging incoming messages addressed to `mycall`
* splitting long messages into 67-character chunks
* sending status and position reports

It works over any `aprs` frame transport: APRS-IS (`aprs.TCP`),
KISS-over-TCP (`aprs.TCPKISS`) or serial KISS (`aprs.SerialKISS`).

## Example

```python
import aprs
from base_aprs_client import Client

with aprs.TCP(
    host="rotate.aprs2.net",
    port=14580,
    user="N0CALL",
    passcode="-1",  # use a real passcode to transmit
    command="filter r/46.1/-122.9/50",
) as transport:
    client = Client(mycall="N0CALL-1", sync_frame_io=transport)
    client.send_status("Hello from base-aprs-client")
    for frame in client.read(min_frames=1):
        print(frame)
```

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

## Development

```
uv sync
uv run pytest
```

CI runs the suite on every supported Python (3.11+). Lint and format with
`uv run ruff check` and `uv run ruff format`, type-check with `uv run ty check`.
