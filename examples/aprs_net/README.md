# APRS Net Control

A web app for running an APRS net, built with [Reflex](https://reflex.dev) on
`base-aprs-client`.

![The net control page during a net](screenshot.png)

Stations check in by sending any APRS message to net control. Each new
station gets one reply ("ACK N0CALL, thanks for checking in to Tuesday Night
Net"). A check-in that names other callsigns ("checking in with N7DEM") logs
those stations too. From the page you can:

* watch check-ins arrive, with each station's last position when it beacons
* log voice check-ins by hand. They're announced in a status ("Voice
  check-ins: W1AW N7DEM ..."), sent once when it fills APRS's 62-character
  status limit, or when you press Send now or close the net
* edit or remove check-ins
* send messages and see when they're acked
* send a bulletin, status and position beacon when the net opens and then on
  a timer. The bulletin can be changed, and switched off and on, while the
  net runs
* see every packet heard and sent
* close the net, which sends a summary status with the end time in the
  server's time zone ("Tuesday Night Net closed 20:15 PST with 12
  check-ins"), and download the check-ins as CSV

Each browser keeps a copy of the net's settings and check-ins in
localStorage. If the server stops mid-net, restart it and the page offers to
reopen the net with the saved check-ins.

It connects to APRS-IS, a KISS TNC on TCP (Dire Wolf listens on
`localhost:8001`), or a serial KISS TNC.

## Running it

```
cd examples/aprs_net
pip install -r requirements.txt
reflex run
```

Then open http://localhost:3000, enter your callsign and connection, and
open the net. To transmit through APRS-IS you need the passcode for your
callsign. With `-1` the net can hear check-ins but its replies are dropped by
the server.

To fill in the form ahead of time, set `APRS_NET_CALL`, `APRS_NET_CONNECTION`
(`aprsis`, `kiss_tcp` or `kiss_serial`), `APRS_NET_ADDRESS`,
`APRS_NET_PASSCODE` and `APRS_NET_NAME`.

### Trying it without a radio

Run the local APRS-IS relay, and point the app at it:

```
python -m base_aprs_client.simulator &
APRS_NET_CALL=N0CALL-10 APRS_NET_ADDRESS=localhost:14588 reflex run
```

Then check in from another terminal with the chat example:

```
python ../chat.py --call N0CALL-1 --to N0CALL-10 --aprsis localhost:14588
```

## How it's put together

* `aprs_net/net.py` has the net logic and no web code: `Net` wraps an
  `AsyncClient`, answers check-ins, runs the timers, and counts changes in
  `version`. `tests/test_aprs_net.py` tests it against the simulator.
* `aprs_net/state.py` is the Reflex state. The one `Net` lives at module
  level, since the server is one radio station shared by every open page.
  Each page polls it once a second and copies it into its state when
  `version` changes. Run the backend with a single worker so every page sees
  the same net.
* `aprs_net/aprs_net.py` is the page.
