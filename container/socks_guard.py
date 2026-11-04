import ipaddress
import json
import os
import selectors
import socket
import socketserver
import struct
import subprocess
import threading


def receive(stream, size):
    data = bytearray()
    while len(data) < size:
        part = stream.recv(size - len(data))
        if not part:
            raise ConnectionError("Client disconnected")
        data.extend(part)
    return bytes(data)


def open_target(target, port, allowed, interface):
    if target not in allowed:
        raise PermissionError("Target not allowed")
    result = subprocess.run(["ip", "-j", "-4", "route", "get", target],
                            capture_output=True, text=True, check=True, timeout=5)
    routes = json.loads(result.stdout)
    if not routes or routes[0].get("dev") != interface or routes[0].get("type", "unicast") != "unicast":
        raise PermissionError("No route through the aTrust tunnel")
    outbound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        outbound.settimeout(8)
        outbound.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
        outbound.connect((target, port))
        return outbound
    except BaseException:
        outbound.close()
        raise


def relay(client, remote):
    client.settimeout(15)
    remote.settimeout(15)
    with selectors.DefaultSelector() as selector:
        selector.register(client, selectors.EVENT_READ, remote)
        selector.register(remote, selectors.EVENT_READ, client)
        while selector.get_map():
            events = selector.select(300)
            if not events:
                return
            for event, _ in events:
                payload = event.fileobj.recv(65536)
                if payload:
                    event.data.sendall(payload)
                else:
                    selector.unregister(event.fileobj)
                    event.data.shutdown(socket.SHUT_WR)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            return
        try:
            self.serve()
        except (OSError, ValueError, subprocess.SubprocessError):
            return
        finally:
            self.server.slots.release()

    def serve(self):
        client = self.request
        client.settimeout(10)
        version, count = receive(client, 2)
        methods = receive(client, count)
        if version != 5 or 0 not in methods:
            client.sendall(b"\x05\xff")
            return
        client.sendall(b"\x05\x00")
        request = receive(client, 4)
        if request != b"\x05\x01\x00\x01":
            client.sendall(b"\x05\x07\x00\x01" + b"\0" * 6)
            return
        target = str(ipaddress.IPv4Address(receive(client, 4)))
        port = struct.unpack("!H", receive(client, 2))[0]
        try:
            remote = open_target(target, port, self.server.allowed, self.server.interface)
        except (OSError, ValueError, subprocess.SubprocessError):
            client.sendall(b"\x05\x02\x00\x01" + b"\0" * 6)
            return
        with remote:
            client.sendall(b"\x05\x00\x00\x01" + b"\0" * 6)
            relay(client, remote)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, allowed, interface):
        self.allowed = frozenset(allowed)
        self.interface = interface
        self.slots = threading.BoundedSemaphore(128)
        super().__init__(address, Handler)


if __name__ == "__main__":
    targets = [str(ipaddress.IPv4Address(value)) for value in os.environ["AI_TARGETS"].split(",")]
    with Server(("0.0.0.0", 1080), targets, os.environ["AI_VPN_INTERFACE"]) as server:
        server.serve_forever()
