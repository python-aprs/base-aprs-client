"""The net control web page."""

import reflex as rx

from .state import CONNECTIONS, NetState


def field(label: str, name: str, placeholder: str = "", **props) -> rx.Component:
    return rx.vstack(
        rx.text(label, size="2", weight="medium"),
        rx.input(
            name=name,
            default_value=NetState.form[name],
            placeholder=placeholder,
            width="100%",
            # keep the form in state, for the environment variables dialog
            on_blur=lambda value: NetState.set_form_field(name, value),
            **props,
        ),
        spacing="1",
        width="100%",
    )


def section(title: str, *children: rx.Component, **props) -> rx.Component:
    return rx.card(
        rx.vstack(rx.heading(title, size="4"), *children, spacing="3", width="100%"),
        width="100%",
        **props,
    )


def local_time(iso: rx.Var, fmt: str = "HH:mm:ss") -> rx.Component:
    """A UTC timestamp shown in the browser's time zone."""
    return rx.moment(iso, format=fmt, local=True)


def env_dialog(**props) -> rx.Component:
    """A button that shows the net's settings as environment variables."""
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(
                "Environment variables",
                type="button",
                variant="soft",
                color_scheme="gray",
                on_click=NetState.show_env,
                **props,
            )
        ),
        rx.dialog.content(
            rx.dialog.title("Environment variables"),
            rx.dialog.description(
                "Set these before ",
                rx.code("reflex run"),
                " to fill in the setup form with this net's settings.",
                size="2",
            ),
            rx.code_block(
                NetState.env_text,
                language="bash",
                wrap_long_lines=True,
                width="100%",
                margin_y="1em",
            ),
            rx.hstack(
                rx.button(
                    "Copy",
                    variant="soft",
                    on_click=[
                        rx.set_clipboard(NetState.env_text),
                        rx.toast.success("Copied"),
                    ],
                ),
                rx.dialog.close(rx.button("Close", variant="soft", color_scheme="gray")),
                justify="end",
            ),
            max_width="40em",
        ),
    )


# -- setup --------------------------------------------------------------------


def setup_view() -> rx.Component:
    return rx.form(
        rx.vstack(
            section(
                "Station",
                rx.grid(
                    field("Your callsign", "mycall", "N0CALL", required=True),
                    rx.vstack(
                        rx.text("Connection", size="2", weight="medium"),
                        rx.select(
                            list(CONNECTIONS),
                            name="connection",
                            default_value=NetState.form["connection"],
                            on_change=lambda value: NetState.set_form_field(
                                "connection", value
                            ),
                            width="100%",
                        ),
                        spacing="1",
                        width="100%",
                    ),
                    field(
                        "Server, TNC or serial port",
                        "address",
                        "rotate.aprs2.net:14580, localhost:8001 or /dev/ttyUSB0",
                    ),
                    field("APRS-IS passcode", "passcode", "-1 is receive only"),
                    field("Digipeater path (radio only)", "path", "WIDE1-1"),
                    columns=rx.breakpoints(initial="1", sm="2"),
                    spacing="3",
                    width="100%",
                ),
            ),
            section(
                "Net",
                rx.grid(
                    field("Net name", "net_name", "Tuesday Night Net"),
                    field("Net control station (NCS)", "ncs", "Callsign"),
                    field(
                        "Reply to check-ins",
                        "checkin_reply",
                        "{calls} and {net} are filled in",
                    ),
                    field(
                        "Message retries (after the first send)",
                        "message_retries",
                        "2",
                        type="number",
                        min=0,
                    ),
                    field(
                        "Minutes between tries",
                        "message_retry_minutes",
                        "5",
                        type="number",
                        min=0.5,
                        step="any",
                    ),
                    columns=rx.breakpoints(initial="1", sm="2"),
                    spacing="3",
                    width="100%",
                ),
            ),
            section(
                "Timed transmissions",
                rx.text(
                    "Each one is sent when the net opens, then every N minutes. "
                    "Leave the minutes at 0 to turn it off.",
                    size="2",
                    color_scheme="gray",
                ),
                rx.grid(
                    field("Bulletin", "bulletin_text", "Net tonight 7 PM on 146.52"),
                    field("Bulletin group", "bulletin_group", "BLN0"),
                    field("Every (minutes)", "bulletin_minutes", "0", type="number"),
                    field("Status", "status_text", "Net control for the Tuesday net"),
                    rx.box(),
                    field("Every (minutes)", "status_minutes", "0", type="number"),
                    field("Beacon latitude", "beacon_lat", "46.1"),
                    field("Beacon longitude", "beacon_lon", "-122.9"),
                    field("Every (minutes)", "beacon_minutes", "0", type="number"),
                    field("Beacon text", "beacon_comment", "Net control, 146.52"),
                    field("Beacon symbol", "beacon_symbol", "/r"),
                    columns=rx.breakpoints(initial="1", sm="3"),
                    spacing="3",
                    width="100%",
                ),
            ),
            rx.cond(
                NetState.restore_info,
                rx.callout.root(
                    rx.callout.icon(rx.icon("history")),
                    rx.callout.text(NetState.restore_info),
                    rx.checkbox(
                        "Restore them when the net opens",
                        name="restore",
                        default_checked=True,
                    ),
                    width="100%",
                ),
            ),
            rx.hstack(
                rx.button("Open the net", type="submit", size="3"),
                env_dialog(size="3"),
                spacing="3",
            ),
            spacing="4",
            width="100%",
        ),
        on_submit=NetState.open_net,
        width="100%",
    )


# -- running ------------------------------------------------------------------


def checkin_edit_row(c: rx.Var) -> rx.Component:
    return rx.table.row(
        rx.table.cell(
            rx.form(
                rx.hstack(
                    rx.input(
                        name="callsign",
                        default_value=c["callsign"],
                        width="8em",
                        auto_focus=True,
                    ),
                    rx.select(
                        ["Voice", "APRS"],
                        name="via",
                        default_value=rx.cond(c["via"] == "aprs", "APRS", "Voice"),
                        size="1",
                    ),
                    rx.input(name="text", default_value=c["text"], flex="1"),
                    rx.button("Save", type="submit", size="1"),
                    rx.button(
                        "Cancel",
                        type="button",
                        size="1",
                        variant="soft",
                        color_scheme="gray",
                        on_click=NetState.cancel_edit,
                    ),
                    width="100%",
                    align="center",
                ),
                on_submit=NetState.save_checkin,
            ),
            col_span=6,
        ),
    )


def checkin_row(c: rx.Var) -> rx.Component:
    return rx.cond(
        NetState.editing == c["callsign"],
        checkin_edit_row(c),
        checkin_view_row(c),
    )


def checkin_view_row(c: rx.Var) -> rx.Component:
    return rx.table.row(
        rx.table.cell(
            rx.cond(
                c["valid"],
                rx.link(
                    c["callsign"],
                    href="https://aprs.fi/#!call=a%2F" + c["callsign"].to(str),
                    is_external=True,
                    weight="bold",
                ),
                rx.text(c["callsign"], weight="bold", color_scheme="gray"),
            )
        ),
        rx.table.cell(
            rx.badge(
                rx.cond(c["via"] == "aprs", "APRS", "voice"),
                color_scheme=rx.cond(c["via"] == "aprs", "blue", "orange"),
                variant="soft",
            )
        ),
        rx.table.cell(local_time(c["time"], "HH:mm")),
        rx.table.cell(c["text"]),
        rx.table.cell(rx.text(c["position"], size="1", color_scheme="gray")),
        rx.table.cell(
            rx.hstack(
                rx.cond(
                    (c["via"] == "voice") & c["valid"].to(bool),
                    rx.tooltip(
                        rx.icon_button(
                            rx.cond(
                                c["announce"],
                                rx.icon("megaphone", size=14),
                                rx.icon("megaphone-off", size=14),
                            ),
                            size="1",
                            variant="ghost",
                            color_scheme=rx.cond(c["announce"], "orange", "gray"),
                            on_click=NetState.toggle_announce(c["callsign"]),
                            aria_label="Include in the voice check-ins status",
                        ),
                        content=rx.cond(
                            c["announced"],
                            "Sent in the voice check-ins status",
                            rx.cond(
                                c["announce"],
                                "In the voice check-ins status; click to leave out",
                                "Left out of the voice check-ins status",
                            ),
                        ),
                    ),
                ),
                rx.icon_button(
                    rx.icon("pencil", size=14),
                    size="1",
                    variant="ghost",
                    color_scheme="gray",
                    on_click=NetState.edit_checkin(c["callsign"]),
                    aria_label="Edit check-in",
                ),
                rx.icon_button(
                    rx.icon("x", size=14),
                    size="1",
                    variant="ghost",
                    color_scheme="gray",
                    on_click=NetState.remove_checkin(c["callsign"]),
                    aria_label="Remove check-in",
                ),
                spacing="3",
            )
        ),
    )


def checkins_card() -> rx.Component:
    return section(
        "Check-ins",
        rx.cond(
            NetState.checkins,
            rx.table.root(
                rx.table.header(
                    rx.table.row(
                        rx.table.column_header_cell("Call"),
                        rx.table.column_header_cell("Via"),
                        rx.table.column_header_cell("Time"),
                        rx.table.column_header_cell("Message"),
                        rx.table.column_header_cell("Position"),
                        rx.table.column_header_cell(""),
                    )
                ),
                rx.table.body(rx.foreach(NetState.checkins, checkin_row)),
                size="1",
                width="100%",
            ),
            rx.text(
                "Waiting for check-ins. Stations check in by sending any message to ",
                rx.code(NetState.mycall),
                ".",
                color_scheme="gray",
            ),
        ),
        rx.form(
            rx.vstack(
                rx.hstack(
                    rx.input(
                        name="callsign",
                        id="checkin_callsign",
                        placeholder="Callsign, or blank or ??",
                        width="12em",
                    ),
                    rx.input(name="note", placeholder="Note (optional)", flex="1"),
                    rx.select(
                        ["Voice", "APRS"],
                        name="via",
                        default_value="Voice",
                        size="2",
                    ),
                    rx.button("Log check-in", type="submit", variant="soft"),
                    width="100%",
                ),
                rx.checkbox(
                    "Include voice check-ins in the status",
                    name="announce",
                    default_checked=True,
                    size="1",
                ),
                width="100%",
            ),
            on_submit=[NetState.add_checkin, rx.set_focus("checkin_callsign")],
            reset_on_submit=True,
        ),
        rx.cond(
            NetState.voice_pending,
            rx.hstack(
                rx.text(
                    "Sent as a status once it fills up: ",
                    rx.code(NetState.voice_pending),
                    size="2",
                    color_scheme="gray",
                ),
                rx.spacer(),
                rx.button(
                    "Send now",
                    size="1",
                    variant="soft",
                    on_click=NetState.announce_voice,
                ),
                width="100%",
                align="center",
            ),
        ),
    )


STATUS_COLORS = {
    "acked": "green",
    "sending": "amber",
    "no ack": "red",
    "rejected": "red",
    "cancelled": "gray",
}


def sent_row(m: rx.Var) -> rx.Component:
    return rx.hstack(
        rx.text(local_time(m["time"], "HH:mm"), size="1", color_scheme="gray"),
        rx.text(m["recipient"], weight="bold", size="2"),
        rx.text(m["text"], size="2", flex="1"),
        rx.badge(
            m["status"],
            color_scheme=rx.match(
                m["status"],
                *[(status, color) for status, color in STATUS_COLORS.items()],
                "gray",
            ),
        ),
        rx.cond(
            m["status"] == "sending",
            rx.button(
                "Cancel",
                size="1",
                variant="ghost",
                color_scheme="red",
                on_click=NetState.cancel_message(m["index"]),
            ),
        ),
        width="100%",
        align="center",
    )


def relay_card() -> rx.Component:
    return section(
        "Relay an announcement",
        rx.form(
            rx.hstack(
                rx.input(name="source", placeholder="From", width="8em"),
                rx.input(
                    name="text", placeholder="Announcement", flex="1", required=True
                ),
                rx.button("Send as status", type="submit", variant="soft"),
                width="100%",
            ),
            on_submit=NetState.relay,
            reset_on_submit=True,
        ),
        rx.text(
            'Sent as "W1AW: ..." statuses, split to fit 62 characters each.',
            size="1",
            color_scheme="gray",
        ),
    )


def message_card() -> rx.Component:
    return section(
        "Messages",
        rx.form(
            rx.hstack(
                rx.input(name="to", placeholder="To", width="8em", required=True),
                rx.input(name="text", placeholder="Message", flex="1", required=True),
                rx.button("Send", type="submit"),
                width="100%",
            ),
            on_submit=NetState.send_message,
            reset_on_submit=True,
        ),
        rx.vstack(rx.foreach(NetState.sent, sent_row), spacing="2", width="100%"),
    )


def bulletin_card() -> rx.Component:
    return section(
        "Bulletin",
        rx.hstack(
            rx.switch(
                checked=NetState.bulletin_on,
                on_change=NetState.toggle_bulletin,
            ),
            rx.text(
                rx.cond(NetState.bulletin_on, "On", "Off"),
                size="2",
            ),
            align="center",
        ),
        rx.form(
            rx.vstack(
                rx.input(
                    name="bulletin_text",
                    default_value=NetState.form["bulletin_text"],
                    placeholder="Net tonight 7 PM on 146.52",
                    width="100%",
                ),
                rx.hstack(
                    rx.input(
                        name="bulletin_group",
                        default_value=NetState.form["bulletin_group"],
                        placeholder="BLN0",
                        width="6em",
                    ),
                    rx.text("every", size="2"),
                    rx.input(
                        name="bulletin_minutes",
                        default_value=NetState.form["bulletin_minutes"],
                        type="number",
                        min=1,
                        width="5em",
                    ),
                    rx.text("minutes", size="2"),
                    rx.spacer(),
                    rx.button("Save", type="submit", variant="soft"),
                    width="100%",
                    align="center",
                ),
                width="100%",
            ),
            on_submit=NetState.save_bulletin,
        ),
    )


def heard_row(h: rx.Var) -> rx.Component:
    return rx.hstack(
        rx.text(local_time(h["time"]), size="1", color_scheme="gray"),
        rx.badge(
            h["direction"],
            color_scheme=rx.cond(h["direction"] == "tx", "blue", "gray"),
            variant="soft",
        ),
        rx.code(h["text"], size="1", variant="ghost", style={"word_break": "break-all"}),
        align="center",
        width="100%",
    )


def traffic_card() -> rx.Component:
    return section(
        "Traffic",
        rx.scroll_area(
            rx.vstack(rx.foreach(NetState.heard, heard_row), spacing="1"),
            type="auto",
            scrollbars="vertical",
            style={"height": "24em"},
        ),
    )


def running_view() -> rx.Component:
    return rx.vstack(
        rx.grid(
            checkins_card(),
            rx.vstack(
                message_card(),
                relay_card(),
                bulletin_card(),
                traffic_card(),
                width="100%",
                spacing="4",
            ),
            columns=rx.breakpoints(initial="1", md="2"),
            spacing="4",
            width="100%",
        ),
        rx.hstack(
            rx.text(
                "Opened at ",
                local_time(NetState.opened, "HH:mm"),
                ". Closing sends: ",
                rx.code(NetState.summary),
                size="2",
                color_scheme="gray",
            ),
            rx.spacer(),
            rx.form(
                rx.hstack(
                    rx.text("NCS", size="2", weight="medium"),
                    rx.input(
                        name="ncs",
                        default_value=NetState.ncs,
                        placeholder="Callsign",
                        width="8em",
                        key=NetState.ncs,
                    ),
                    rx.button("Save", type="submit", variant="soft", size="2"),
                    align="center",
                ),
                on_submit=NetState.save_ncs,
            ),
            env_dialog(),
            rx.button("Close the net", color_scheme="red", on_click=NetState.close_net),
            width="100%",
            align="center",
            wrap="wrap",
            spacing="3",
        ),
        spacing="4",
        width="100%",
    )


# -- closed -------------------------------------------------------------------


def closed_view() -> rx.Component:
    return section(
        NetState.summary,
        rx.cond(NetState.ncs, rx.text("Net control: ", rx.text.strong(NetState.ncs))),
        rx.table.root(
            rx.table.body(
                rx.foreach(
                    NetState.checkins,
                    lambda c: rx.table.row(
                        rx.table.cell(c["callsign"], font_weight="bold"),
                        rx.table.cell(c["via"]),
                        rx.table.cell(local_time(c["time"], "HH:mm")),
                        rx.table.cell(c["text"]),
                    ),
                )
            ),
            size="1",
        ),
        rx.hstack(
            rx.button("Download check-ins (CSV)", on_click=NetState.download_checkins),
            rx.button("Start a new net", variant="soft", on_click=NetState.new_net),
        ),
    )


# -- page ---------------------------------------------------------------------


def header() -> rx.Component:
    return rx.hstack(
        rx.heading(
            rx.cond(NetState.phase == "setup", "APRS Net Control", NetState.net_name),
            size="7",
        ),
        rx.match(
            NetState.phase,
            (
                "running",
                rx.cond(
                    NetState.connected,
                    rx.badge(NetState.mycall + " on the air", color_scheme="green"),
                    rx.badge("Reconnecting…", color_scheme="amber"),
                ),
            ),
            ("closed", rx.badge("Closed", color_scheme="gray")),
            rx.fragment(),
        ),
        rx.cond(
            NetState.phase == "running",
            rx.badge(
                NetState.checkins.length().to(str)
                + " checked in: "
                + NetState.aprs_count.to(str)
                + " APRS, "
                + NetState.voice_count.to(str)
                + " voice",
                variant="soft",
            ),
        ),
        rx.spacer(),
        rx.color_mode.button(),
        width="100%",
        align="center",
    )


def index() -> rx.Component:
    return rx.container(
        rx.vstack(
            header(),
            rx.match(
                NetState.phase,
                ("running", running_view()),
                ("closed", closed_view()),
                setup_view(),
            ),
            # poll the net once a second while the page is open
            rx.moment(interval=1000, on_change=NetState.tick, display="none"),
            spacing="5",
            width="100%",
        ),
        size="4",
        padding_y="2em",
    )


app = rx.App()
app.add_page(index, title="APRS Net Control", on_load=NetState.load)
