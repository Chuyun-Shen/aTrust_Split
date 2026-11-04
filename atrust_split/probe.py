import ipaddress
import socket
import struct


def receive(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        data = stream.recv(size - len(chunks))
        if not data:
            raise ConnectionError("SOCKS connection closed early")
        chunks.extend(data)
    return bytes(chunks)


def connect(port, target, target_port, timeout=8):
    stream = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        stream.settimeout(timeout)
        stream.sendall(b"\x05\x01\x00")
        if receive(stream, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS server rejected the handshake")
        stream.sendall(b"\x05\x01\x00\x01" + ipaddress.IPv4Address(target).packed + struct.pack("!H", target_port))
        response = receive(stream, 4)
        if response != b"\x05\x00\x00\x01":
            raise ConnectionError("Target is denied, disconnected or unreachable through the VPN")
        receive(stream, 6)
        return stream
    except BaseException:
        stream.close()
        raise
