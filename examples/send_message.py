import argparse
import logging

from aprs import TCP
from base_aprs_client import Client


logging.basicConfig(level=logging.DEBUG)


def split_ip_port(ip_and_port, default_port=None):
    ip, _, port = ip_and_port.partition(":")
    if not port:
        port = default_port
    if port:
        port = int(port)
    return (ip, port)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mycall", required=True)
    parser.add_argument("--tocall", required=True)
    parser.add_argument("--path", default="WIDE2-1")
    parser.add_argument("-m", "--msg")
    parser.add_argument("--wait", action="store_true", help="wait for message acknowledgement")
    parser.add_argument("--aprsis", required=True)
    parser.add_argument("--aprsis-passcode")
        
    args = parser.parse_args()
    aprsis_ip, aprsis_port = split_ip_port(args.aprsis, default_port=14580)
    with TCP(aprsis_ip, aprsis_port, args.mycall, args.aprsis_passcode) as transport:
        client = Client(mycall=args.mycall, sync_frame_io=transport, default_path=args.path)
        msg_future = client.send_message(recipient=args.tocall, body=args.msg, ack=args.wait)
        if msg_future is not None:
            while not msg_future.done():
                print(client.read(min_frames=1))
            print(msg_future.result())
        else:
            print(client.read())


if __name__ == "__main__":
    main()