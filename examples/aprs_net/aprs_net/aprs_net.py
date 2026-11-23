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
                    field(
                        "Reply to check-ins",
                        "checkin_reply",
                        "{calls} and {net} are filled in",
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
                    columns=rx.breakpoints(initial="1", sm="3"),
                    spacing="3",
                    width="100%",
                ),
            ),
            rx.button("Open the net", type="submit", size="3"),
            spacing="4",
            width="100%",
        ),
        on_submit=NetState.open_net,
        width="100%",
    )


# -- running ------------------------------------------------------------------


def checkin_row(c: rx.Var) -> rx.Component:
    return rx.table.row(
        rx.table.cell(
            rx.link(
                c["callsign"],
                href="https://aprs.fi/#!call=a%2F" + c["callsign"].to(str),
                is_external=True,
                weight="bold",
            )
        ),
        rx.table.cell(local_time(c["time"], "HH:mm")),
        rx.table.cell(c["text"]),
        rx.table.cell(rx.text(c["position"], size="1", color_scheme="gray")),
        rx.table.cell(
            rx.icon_button(
                rx.icon("x", size=14),
                size="1",
                variant="ghost",
                color_scheme="gray",
                on_click=NetState.remove_checkin(c["callsign"]),
                aria_label="Remove check-in",
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
            rx.hstack(
                rx.input(name="callsign", placeholder="Callsign", required=True),
                rx.input(name="note", placeholder="Note (optional)", flex="1"),
                rx.button("Log check-in", type="submit", variant="soft"),
                width="100%",
            ),
            on_submit=NetState.add_checkin,
            reset_on_submit=True,
        ),
    )


STATUS_COLORS = {
    "acked": "green",
    "sending": "amber",
    "no ack": "red",
    "rejected": "red",
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
        width="100%",
        align="center",
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
            rx.vstack(message_card(), traffic_card(), width="100%", spacing="4"),
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
            rx.button("Close the net", color_scheme="red", on_click=NetState.close_net),
            width="100%",
            align="center",
        ),
        spacing="4",
        width="100%",
    )


# -- closed -------------------------------------------------------------------


def closed_view() -> rx.Component:
    return section(
        NetState.summary,
        rx.table.root(
            rx.table.body(
                rx.foreach(
                    NetState.checkins,
                    lambda c: rx.table.row(
                        rx.table.cell(c["callsign"], font_weight="bold"),
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
            rx.badge(NetState.checkins.length().to(str) + " checked in", variant="soft"),
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
