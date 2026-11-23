"""
Reflex state: the browser's view of the one ``Net`` this server runs.

The net lives at module level, not in ``NetState``, because there is one
radio station per server, shared by everyone who opens the page. Each page
polls it once a second (``tick``) and copies it into state when it changes.
Run the backend with a single worker so every page sees the same net.
"""

import os

import reflex as rx

from .net import Net, Settings

NET: Net | None = None

CONNECTIONS = {"APRS-IS": "aprsis", "KISS TCP": "kiss_tcp", "KISS serial": "kiss_serial"}


def default_settings() -> Settings:
    """Settings prefilled from APRS_NET_* environment variables."""
    env = os.environ.get
    defaults = Settings()
    return Settings(
        mycall=env("APRS_NET_CALL", defaults.mycall),
        connection=env("APRS_NET_CONNECTION", defaults.connection),
        address=env("APRS_NET_ADDRESS", defaults.address),
        passcode=env("APRS_NET_PASSCODE", defaults.passcode),
        net_name=env("APRS_NET_NAME", defaults.net_name),
    )


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _iso(dt) -> str:
    return dt.isoformat(timespec="seconds")


class NetState(rx.State):
    phase: str = "setup"  # setup, running or closed
    connected: bool = False
    error: str = ""
    form: dict[str, str] = {}
    net_name: str = ""
    mycall: str = ""
    opened: str = ""
    summary: str = ""
    checkins: list[dict[str, str]] = []
    heard: list[dict[str, str]] = []
    sent: list[dict[str, str]] = []
    version: int = -1

    @rx.event
    def load(self):
        """Fill the setup form and show the current net, if one is open."""
        s = NET.settings if NET is not None else default_settings()
        label = {v: k for k, v in CONNECTIONS.items()}
        self.form = {
            "mycall": s.mycall,
            "connection": label.get(s.connection, "APRS-IS"),
            "address": s.address,
            "passcode": s.passcode,
            "path": s.path,
            "net_name": s.net_name,
            "checkin_reply": s.checkin_reply,
            "bulletin_group": s.bulletin_group,
            "bulletin_text": s.bulletin_text,
            "bulletin_minutes": f"{s.bulletin_minutes:g}",
            "status_text": s.status_text,
            "status_minutes": f"{s.status_minutes:g}",
            "beacon_lat": "" if s.beacon_lat is None else str(s.beacon_lat),
            "beacon_lon": "" if s.beacon_lon is None else str(s.beacon_lon),
            "beacon_symbol": s.beacon_symbol,
            "beacon_minutes": f"{s.beacon_minutes:g}",
        }
        self.version = -1
        self._sync()

    @rx.event
    def tick(self, _=None):
        if NET is not None and NET.version != self.version:
            self._sync()

    def _sync(self):
        net = NET
        if net is None:
            self.phase = "setup"
            return
        self.version = net.version
        self.phase = "running" if net.running else "closed"
        self.connected = net.connected
        self.net_name = net.settings.net_name
        self.mycall = net.settings.mycall.upper()
        self.opened = _iso(net.opened) if net.opened else ""
        self.summary = net.summary()
        self.checkins = [
            {
                "callsign": c.callsign,
                "time": _iso(c.time),
                "text": c.text,
                "heard_from": c.heard_from,
                "position": "{:.4f}, {:.4f}".format(*net.positions[c.callsign])
                if c.callsign in net.positions
                else "",
            }
            for c in net.checkins.values()
        ]
        self.heard = [
            {"time": _iso(h.time), "direction": h.direction, "text": h.text}
            for h in reversed(net.heard)
        ]
        self.sent = [
            {
                "time": _iso(m.time),
                "recipient": m.recipient,
                "text": m.text,
                "status": m.status,
            }
            for m in reversed(net.sent[-50:])
        ]

    @rx.event
    async def open_net(self, form_data: dict[str, str]):
        global NET  # noqa: PLW0603 - one net per server
        if NET is not None and NET.running:
            return rx.toast.error("A net is already open")
        self.form = form_data
        f = {k: v.strip() for k, v in form_data.items()}
        if not f.get("mycall"):
            return rx.toast.error("Enter your callsign")
        settings = Settings(
            mycall=f["mycall"].upper(),
            connection=CONNECTIONS.get(f.get("connection", ""), "aprsis"),
            address=f.get("address", ""),
            passcode=f.get("passcode") or "-1",
            path=f.get("path", ""),
            net_name=f.get("net_name") or "APRS Net",
            checkin_reply=f.get("checkin_reply") or Settings().checkin_reply,
            bulletin_group=f.get("bulletin_group") or "BLN0",
            bulletin_text=f.get("bulletin_text", ""),
            bulletin_minutes=_float(f.get("bulletin_minutes", "")) or 0,
            status_text=f.get("status_text", ""),
            status_minutes=_float(f.get("status_minutes", "")) or 0,
            beacon_lat=_float(f.get("beacon_lat", "")),
            beacon_lon=_float(f.get("beacon_lon", "")),
            beacon_symbol=f.get("beacon_symbol") or "/r",
            beacon_minutes=_float(f.get("beacon_minutes", "")) or 0,
        )
        net = Net(settings)
        try:
            await net.open()
        except (OSError, ValueError) as exc:
            return rx.toast.error(f"Could not connect: {exc}")
        NET = net
        self._sync()

    @rx.event
    async def close_net(self):
        if NET is not None:
            await NET.close()
            self._sync()

    @rx.event
    def new_net(self):
        global NET  # noqa: PLW0603
        if NET is not None and not NET.running:
            NET = None
        self.load()

    @rx.event
    def add_checkin(self, form_data: dict[str, str]):
        if NET is None or not NET.running:
            return
        call = form_data.get("callsign", "")
        if not NET.add_checkin(call, form_data.get("note") or "manual"):
            return rx.toast.warning(f"{call.upper() or 'That'} is already checked in")
        self._sync()

    @rx.event
    def remove_checkin(self, callsign: str):
        if NET is not None:
            NET.remove_checkin(callsign)
            self._sync()

    @rx.event
    def send_message(self, form_data: dict[str, str]):
        if NET is None or not NET.running:
            return
        to, text = form_data.get("to", "").strip(), form_data.get("text", "").strip()
        if not to or not text:
            return rx.toast.error("Enter a callsign and a message")
        NET.send_message(to, text)
        self._sync()

    @rx.event
    def download_checkins(self):
        if NET is not None:
            day = NET.opened.strftime("%Y-%m-%d") if NET.opened else "net"
            return rx.download(data=NET.checkins_csv(), filename=f"checkins-{day}.csv")
