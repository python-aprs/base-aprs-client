"""
Reflex state: the browser's view of the one ``Net`` this server runs.

The net lives at module level, not in ``NetState``, because there is one
radio station per server, shared by everyone who opens the page. Each page
polls it once a second (``tick``) and copies it into state when it changes.
Run the backend with a single worker so every page sees the same net.

Each browser also keeps a copy of the net's settings and check-ins in
localStorage. If the server restarts mid-net, the setup page offers to
reopen the net with them. The settings of the last net opened in a browser
fill in the setup form there next time, ahead of the APRS_NET_* environment
variables.
"""

import json
import os
import shlex

import reflex as rx

from base_aprs_client import NotConnected

from .net import Net, Settings, base_call, valid_call

NET: Net | None = None

CONNECTIONS = {"APRS-IS": "aprsis", "KISS TCP": "kiss_tcp", "KISS serial": "kiss_serial"}


def env_name(key: str) -> str:
    """The environment variable for a setup form field."""
    return {"mycall": "APRS_NET_CALL", "net_name": "APRS_NET_NAME"}.get(
        key, f"APRS_NET_{key.upper()}"
    )


def default_form() -> dict[str, str]:
    """The setup form, prefilled from APRS_NET_* environment variables."""
    label = {v: k for k, v in CONNECTIONS.items()}
    form = _form(Settings())
    for key in form:
        value = os.environ.get(env_name(key))
        if value is not None:
            form[key] = label.get(value, value) if key == "connection" else value
    return form


def default_settings() -> Settings:
    """Settings prefilled from APRS_NET_* environment variables."""
    return settings_from_form(default_form())


def env_text(form: dict[str, str]) -> str:
    """The form's settings as shell ``export`` lines."""
    defaults = _form(Settings())
    lines = []
    for key, value in form.items():
        if key not in defaults:
            continue
        if key == "connection":
            value = CONNECTIONS.get(value, value)
        always = key in ("mycall", "connection", "address", "net_name")
        if always or value != defaults[key]:
            lines.append(f"export {env_name(key)}={shlex.quote(value)}")
    return "\n".join(lines)


def settings_from_form(form_data: dict[str, str]) -> Settings:
    f = {k: v.strip() for k, v in form_data.items()}
    defaults = Settings()
    bulletin_minutes = _float(f.get("bulletin_minutes", "")) or 0
    retries = _float(f.get("message_retries", ""))
    retries = None if retries is None or retries < 0 else retries
    return Settings(
        mycall=f.get("mycall", "").upper() or defaults.mycall,
        connection=CONNECTIONS.get(f.get("connection", ""), "aprsis"),
        address=f.get("address", ""),
        passcode=f.get("passcode") or "-1",
        path=f.get("path", ""),
        net_name=f.get("net_name") or defaults.net_name,
        checkin_reply=f.get("checkin_reply") or defaults.checkin_reply,
        ncs=f.get("ncs", "").upper(),
        message_retries=int(retries) if retries is not None else defaults.message_retries,
        message_retry_minutes=_float(f.get("message_retry_minutes", ""))
        or defaults.message_retry_minutes,
        bulletin_group=f.get("bulletin_group") or "BLN0",
        bulletin_text=f.get("bulletin_text", ""),
        bulletin_minutes=bulletin_minutes,
        bulletin_on=bool(f.get("bulletin_text")) and bulletin_minutes > 0,
        status_text=f.get("status_text", ""),
        status_minutes=_float(f.get("status_minutes", "")) or 0,
        beacon_lat=_float(f.get("beacon_lat", "")),
        beacon_lon=_float(f.get("beacon_lon", "")),
        beacon_symbol=f.get("beacon_symbol") or "/r",
        beacon_comment=f.get("beacon_comment", ""),
        beacon_minutes=_float(f.get("beacon_minutes", "")) or 0,
    )


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _iso(dt) -> str:
    return dt.isoformat(timespec="seconds")


def _form(s: Settings) -> dict[str, str]:
    """The setup form's fields for ``s``."""
    label = {v: k for k, v in CONNECTIONS.items()}
    return {
        "mycall": s.mycall,
        "connection": label.get(s.connection, "APRS-IS"),
        "address": s.address,
        "passcode": s.passcode,
        "path": s.path,
        "net_name": s.net_name,
        "checkin_reply": s.checkin_reply,
        "ncs": s.ncs,
        "message_retries": str(s.message_retries),
        "message_retry_minutes": f"{s.message_retry_minutes:g}",
        "bulletin_group": s.bulletin_group,
        "bulletin_text": s.bulletin_text,
        "bulletin_minutes": f"{s.bulletin_minutes:g}",
        "status_text": s.status_text,
        "status_minutes": f"{s.status_minutes:g}",
        "beacon_lat": "" if s.beacon_lat is None else str(s.beacon_lat),
        "beacon_lon": "" if s.beacon_lon is None else str(s.beacon_lon),
        "beacon_symbol": s.beacon_symbol,
        "beacon_comment": s.beacon_comment,
        "beacon_minutes": f"{s.beacon_minutes:g}",
    }


class NetState(rx.State):
    phase: str = "setup"  # setup, running or closed
    connected: bool = False
    error: str = ""
    form: dict[str, str] = {}
    net_name: str = ""
    mycall: str = ""
    opened: str = ""
    summary: str = ""
    ncs: str = ""
    aprs_count: int = 0
    voice_count: int = 0
    checkins: list[dict[str, str]] = []
    heard: list[dict[str, str]] = []
    sent: list[dict[str, str]] = []
    voice_pending: str = ""
    """The voice check-in status waiting to be sent, if any."""
    bulletin_on: bool = True
    editing: str = ""
    """The callsign of the check-in being edited."""
    backup: str = rx.LocalStorage("", name="aprs_net_backup")
    """This browser's copy of the net, as JSON."""
    restore_info: str = ""
    last_form: str = rx.LocalStorage("", name="aprs_net_settings")
    """The setup form of the last net opened in this browser, as JSON."""
    env_text: str = ""
    """Describes the backup the setup page offers to restore."""
    version: int = -1

    def _saved(self) -> dict | None:
        """The backup of a net that was still open, if this browser has one."""
        try:
            saved = json.loads(self.backup) if self.backup else None
        except ValueError:
            return None
        if not isinstance(saved, dict) or saved.get("closed"):
            return None
        return saved

    def _last_form(self) -> dict[str, str]:
        try:
            saved = json.loads(self.last_form) if self.last_form else {}
        except ValueError:
            return {}
        if not isinstance(saved, dict):
            return {}
        return {k: v for k, v in saved.items() if isinstance(v, str)}

    @rx.event
    def set_form_field(self, key: str, value: str):
        self.form = {**self.form, key: value}

    @rx.event
    def show_env(self):
        form = _form(NET.settings) if NET is not None else self.form
        self.env_text = env_text(form)

    @rx.event
    def load(self):
        """Fill the setup form and show the current net, if one is open."""
        self.restore_info = ""
        if NET is not None:
            settings = NET.settings
        elif (saved := self._saved()) is not None:
            # the server lost the net; offer to pick up where it left off
            restored = Net.restore(saved)
            settings = restored.settings
            count = len(restored.checkins)
            self.restore_info = (
                f"{count} check-in{' was' if count == 1 else 's were'} "
                f"from {settings.net_name} saved in this browser."
            )
        else:
            self.form = {**default_form(), **self._last_form()}
            self.version = -1
            self._sync()
            return
        self.form = _form(settings)
        self.version = -1
        self._sync()

    @rx.event
    def tick(self, _=None):
        if NET is None and self.phase != "setup":
            self.load()  # the server restarted: offer the backup
        elif NET is not None and NET.version != self.version:
            self._sync()

    def _sync(self):
        net = NET
        if net is None:
            self.phase = "setup"
            return
        self.version = net.version
        self.backup = json.dumps(net.backup())
        self.phase = "running" if net.running else "closed"
        self.bulletin_on = net.settings.bulletin_on
        self.voice_pending = net.voice_status() if net.voice_pending else ""
        self.connected = net.connected
        self.net_name = net.settings.net_name
        self.mycall = net.settings.mycall.upper()
        self.opened = _iso(net.opened) if net.opened else ""
        self.summary = net.summary()
        self.ncs = net.settings.ncs
        counts = net.counts()
        self.aprs_count, self.voice_count = counts["aprs"], counts["voice"]
        self.checkins = [
            {
                "callsign": c.callsign,
                "time": _iso(c.time),
                "text": c.text,
                "heard_from": c.heard_from,
                "via": c.via,
                # "" for false: the page's rx.cond treats any other text as true
                "announce": "yes" if c.announce else "",
                "announced": "yes" if c.announced else "",
                "valid": "yes" if valid_call(c.callsign) else "",
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
                "index": str(i),
                "time": _iso(m.time),
                "recipient": m.recipient,
                "text": m.text,
                "status": m.status,
            }
            for i, m in reversed(list(enumerate(net.sent))[-50:])
        ]

    @rx.event
    async def open_net(self, form_data: dict[str, str]):
        global NET  # noqa: PLW0603 - one net per server
        if NET is not None and NET.running:
            return rx.toast.error("A net is already open")
        f = {k: v.strip() for k, v in form_data.items()}
        self.form = {k: v for k, v in f.items() if k != "restore"}
        if not f.get("mycall"):
            return rx.toast.error("Enter your callsign")
        settings = settings_from_form(f)
        saved = self._saved() if f.get("restore") else None
        net = Net.restore(saved, settings) if saved else Net(settings)
        try:
            await net.open()
        except (OSError, ValueError) as exc:
            return rx.toast.error(f"Could not connect: {exc}")
        NET = net
        self.restore_info = ""
        self.last_form = json.dumps(_form(settings))
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
            self.backup = ""
        self.load()

    @rx.event
    def add_checkin(self, form_data: dict[str, str]):
        if NET is None or not NET.running:
            return
        call = form_data.get("callsign", "")
        key = NET.add_checkin(
            call,
            form_data.get("note", "").strip(),
            via="aprs" if form_data.get("via") == "APRS" else "voice",
            announce=bool(form_data.get("announce")),
        )
        if key is None:
            return rx.toast.warning(f"{base_call(call)} is already checked in")
        self._sync()

    @rx.event
    def edit_checkin(self, callsign: str):
        self.editing = callsign

    @rx.event
    def cancel_edit(self):
        self.editing = ""

    @rx.event
    def save_checkin(self, form_data: dict[str, str]):
        if NET is None or not self.editing:
            return
        call = form_data.get("callsign", "")
        via = {"APRS": "aprs", "Voice": "voice"}.get(form_data.get("via", ""))
        if not NET.edit_checkin(
            self.editing, call, form_data.get("text", "").strip(), via=via
        ):
            return rx.toast.error(f"{base_call(call) or 'That'} is already checked in")
        self.editing = ""
        self._sync()

    @rx.event
    def toggle_announce(self, callsign: str):
        if NET is not None and callsign in NET.checkins:
            NET.set_announce(callsign, not NET.checkins[callsign].announce)
            self._sync()

    @rx.event
    def cancel_message(self, index: str):
        if NET is not None and 0 <= int(index) < len(NET.sent):
            NET.cancel_message(NET.sent[int(index)])
            self._sync()

    @rx.event
    def relay(self, form_data: dict[str, str]):
        if NET is None or not NET.running:
            return
        text = form_data.get("text", "").strip()
        if not text:
            return rx.toast.error("Enter the announcement")
        try:
            sent = NET.relay_announcement(form_data.get("source", ""), text)
        except NotConnected:
            return rx.toast.error("Not connected; try again when it reconnects")
        self._sync()
        parts = f" in {len(sent)} parts" if len(sent) > 1 else ""
        return rx.toast.success(f"Announcement sent{parts}")

    @rx.event
    def save_ncs(self, form_data: dict[str, str]):
        if NET is not None:
            NET.set_ncs(form_data.get("ncs", ""))
            self.form = {**self.form, "ncs": NET.settings.ncs}
            self.last_form = json.dumps(_form(NET.settings))
            self._sync()

    @rx.event
    def announce_voice(self):
        if NET is not None and not NET.announce_voice_checkins():
            return rx.toast.error("Not connected; try again when it reconnects")
        self._sync()

    @rx.event
    def save_bulletin(self, form_data: dict[str, str]):
        if NET is None or not NET.running:
            return
        f = {k: v.strip() for k, v in form_data.items()}
        NET.set_bulletin(
            f.get("bulletin_text", ""),
            f.get("bulletin_group", ""),
            _float(f.get("bulletin_minutes", "")) or 0,
            NET.settings.bulletin_on,
        )
        self.form = {**self.form, **f}
        self.last_form = json.dumps(_form(NET.settings))
        self._sync()
        if "bulletin" in NET.timers:
            return rx.toast.success("Bulletin sent")

    @rx.event
    def toggle_bulletin(self, on: bool):
        if NET is None or not NET.running:
            return
        s = NET.settings
        NET.set_bulletin(s.bulletin_text, s.bulletin_group, s.bulletin_minutes, on)
        self._sync()
        if on and "bulletin" not in NET.timers:
            return rx.toast.error("Enter the bulletin and how often to send it")

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
