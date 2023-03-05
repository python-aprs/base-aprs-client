"""
How to run the net:
    MYCALL=N0CALL-3 tox
    to respond to messages
      python3 n7dem_5.py KF7HVM "ACK thanks for checking in"
    to close the net
      ## STOP the server
      python3 n7dem_5.py status


"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import logging
from pathlib import Path
import re
import time
from threading import Event

import appdirs
import attrs

import aprs
from aprs import APRSFrame, Message

from base_aprs_client import Client

APPNAME = "aprs_net"
VERSION = "0.1"

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger("aprs_net.net")


CALLSIGN_REX = re.compile(rb"\b([A-Z]{1,3}[0-9][A-Z]{1,3})\b")


def split_ip_port(ip_and_port, default_port=None):
    ip, _, port = ip_and_port.partition(":")
    if not port:
        port = default_port
    if port:
        port = int(port)
    return (ip, port)


@attrs.define
class TimestampTracker:
    ts_id = attrs.field()

    @property
    def _marker_file(self):
        user_state_dir = Path(appdirs.user_state_dir(appname=APPNAME, version=VERSION))
        user_state_dir.mkdir(parents=True, exist_ok=True)
        return (user_state_dir / self.ts_id).with_suffix(".ts.txt")

    @property
    def time(self):
        try:
            return float(self._marker_file.read_text())
        except:
            log.info(f"Could not get timestamp for {self.ts_id}")
            return 0

    @time.setter
    def time(self, value):
        self._marker_file.write_text(str(value))


@attrs.define
class APRSNetClient(Client):
    checkin_text = attrs.field(default="ACK {tocall}")
    all_checkins = attrs.field(factory=dict)
    _timers = attrs.field(factory=list)
    _tpe = attrs.field(factory=ThreadPoolExecutor)
    _stop_all = attrs.field(factory=Event)

    def schedule_periodic(self, cb, period_sec, identifier):
        """
        Schedule periodic TX of a packet every `period_sec` seconds.

        Write the last update epoch timestamp to last_update_marker_file for
        persistance across invocations.

        The returned function is intended to be used as the target of thread.

        The type_name is used for logging and has no other purpose.
        """

        def _handle():
            ts = TimestampTracker(ts_id=identifier)
            while True:
                next_tx_in = ts.time - time.time() + period_sec
                if next_tx_in > 0:
                    log.debug("Will {} in {} seconds".format(identifier, round(next_tx_in)))
                    if self._stop_all.wait(timeout=next_tx_in):
                        return
                cb()
                ts.time = time.time()

        th = self._tpe.submit(_handle)
        self._timers.append(th)
        return th

    def on_frame(self, frame: APRSFrame) -> None:
        log.debug(frame)
        return super().on_frame(frame)

    def on_message(self, message: Message, frame: APRSFrame) -> None:
        matches = CALLSIGN_REX.findall(message.text) 
        if frame.source.callsign not in matches:
            matches.append(frame.source.callsign)
        new_checkins = [m.decode() for m in matches if m.decode() not in self.all_checkins]
        if new_checkins:
            body = self.checkin_text.format(tocall=" ".join(new_checkins))
            self.send_message(
                recipient=frame.source,
                body=body,
            )
            log.info(body)
            for callsign in new_checkins:
                self.all_checkins[callsign] = frame
            self.log_checkins()
        return super().on_message(message, frame)

    def is_alive(self):
        return not all(t.done() for t in self._timers)
    
    def stop(self):
        self._stop_all.set()
        for t in self._timers:
            t.result()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.stop()

    def log_checkins(self):
        log.info("Checkins: %s", ", ".join(self.all_checkins))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mycall", required=True, help="Callsign used to sign outgoing packets")
    parser.add_argument("--path", default="WIDE2-1", help="Comma separated list of path (digipeater).")
    parser.add_argument("--kiss", default=None, help="ip:port for KISS over TCP connection")
    parser.add_argument("--aprsis", default=None, help="ip:port for APRS-IS TCP connection")
    parser.add_argument("--aprsis-passcode", default=None, help="Passcode must match mycall in order to TX via APRS-IS")
    parser.add_argument("--bulletin-period", default="7200", type=int, help="Minimum time in seconds between sending bulletin message. 0 to disable.")
    parser.add_argument("--bulletin-group", default="BLN0", help="Bulletin group, like BLN0")
    parser.add_argument("--bulletin-text", default=None, help="Bulletin text, 67 characters max")
    parser.add_argument("--beacon-period", default="1800", type=int, help="Minimum time in seconds between sending beacon. 0 to disable.")
    parser.add_argument("--beacon-packet", default=None, help="Position packet, raw.")
    parser.add_argument("--status-period", default="1800", type=int, help="Minimum time in seconds between sending status. 0 to disable.")
    parser.add_argument("--status-text", default=None, help="Status text, 62 characters max")
    parser.add_argument("--report-checkins", default=None, help="Number of checkins to report")
    parser.add_argument("--end-time", default=time.strftime("%H:%M"), help="End time to report (defaults to now)")
    parser.add_argument("--checkin-text", default="ACK {tocall}, thanks for checking in", help="Message text, {tocall} will be replaced")

    args = parser.parse_args()

    # determine the transport and connect to it
    if args.kiss is not None:
        kiss_ip, kiss_port = split_ip_port(args.kiss, default_port=8001)
        transport = aprs.TCPKISS(kiss_ip, kiss_port, strip_df_start=True) 
    elif args.aprsis is not None:
        aprsis_ip, aprsis_port = split_ip_port(args.aprsis, default_port=14580)
        transport = aprs.TCP(aprsis_ip, aprsis_port, args.mycall, args.aprsis_passcode)
    else:
        raise RuntimeError("Must specify --kiss or --aprsis to proceed.")

    with transport, APRSNetClient(checkin_text=args.checkin_text, mycall=args.mycall, sync_frame_io=transport, default_path=args.path) as client:
        if args.report_checkins is not None:
            # report checkins and exit
            client.send_status(f'>ACS Net: Mon 20:00 - {args.end_time} w/ {args.report_checkins} checkins')
            return
        # otherwise start the net server
        if args.bulletin_text and args.bulletin_period > 0:
            client.schedule_periodic(
                cb=partial(client.send_message, args.bulletin_group, args.bulletin_text),
                period_sec=args.bulletin_period,
                identifier="bulletin",
            )
        if args.beacon_packet and args.beacon_period > 0:
            beacon = aprs.PositionReport.from_bytes(args.beacon_packet.encode())
            client.schedule_periodic(
                cb=partial(client.write, client.prepare_frame(beacon)),
                period_sec=args.beacon_period,
                identifier="beacon",
            )
        if args.status_text and args.status_period > 0:
            client.schedule_periodic(
                cb=partial(client.send_status, args.status_text),
                period_sec=args.status_period,
                identifier="status",
            )
        while client.is_alive():
            client.read(min_frames=1)


if __name__ == "__main__":
    main()