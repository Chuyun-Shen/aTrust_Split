import importlib.util
import json
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import Mock, patch

from atrust_split.probe import connect, receive

SPEC = importlib.util.spec_from_file_location("socks_guard", Path(__file__).resolve().parents[1] / "container/socks_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def test_denied_ip_does_not_even_query_routes(self):
        with patch.object(guard.subprocess, "run") as command:
            with self.assertRaises(PermissionError):
                guard.open_target("192.0.2.1", 80, {"10.1.2.3"}, "utun7")
        command.assert_not_called()

    def test_non_vpn_route_does_not_open_socket(self):
        result = Mock(stdout=json.dumps([{"dev": "eth0"}]))
        with patch.object(guard.subprocess, "run", return_value=result), patch.object(guard.socket, "socket") as stream:
            with self.assertRaises(PermissionError):
                guard.open_target("10.1.2.3", 22, {"10.1.2.3"}, "utun7")
        stream.assert_not_called()

    def test_outbound_is_bound_to_vpn_before_connect(self):
        events = []
        stream = Mock()
        stream.setsockopt.side_effect = lambda *args: events.append(("bind", args))
        stream.connect.side_effect = lambda *args: events.append(("connect", args))
        result = Mock(stdout=json.dumps([{"dev": "utun7"}]))
        with patch.object(guard.subprocess, "run", return_value=result), \
             patch.object(guard.socket, "socket", return_value=stream), \
             patch.object(guard.socket, "SO_BINDTODEVICE", 25, create=True):
            self.assertIs(guard.open_target("10.1.2.3", 22, {"10.1.2.3"}, "utun7"), stream)
        self.assertEqual([event[0] for event in events], ["bind", "connect"])
        self.assertEqual(events[0][1][-1], b"utun7\0")

    def test_socket_closes_on_bind_failure_without_fallback(self):
        stream = Mock()
        stream.setsockopt.side_effect = OSError("no tunnel")
        with patch.object(guard.subprocess, "run", return_value=Mock(stdout='[{"dev":"utun7"}]')), \
             patch.object(guard.socket, "socket", return_value=stream), \
             patch.object(guard.socket, "SO_BINDTODEVICE", 25, create=True):
            with self.assertRaises(OSError):
                guard.open_target("10.1.2.3", 22, {"10.1.2.3"}, "utun7")
        stream.connect.assert_not_called()
        stream.close.assert_called_once()

    def server(self):
        server = guard.Server(("127.0.0.1", 0), {"10.1.2.3"}, "utun7")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_actual_socks_handshake_rejects_denied_address(self):
        port = self.server()
        with patch.object(guard.subprocess, "run") as command:
            with self.assertRaises(ConnectionError):
                connect(port, "192.0.2.1", 22)
        command.assert_not_called()

    def test_actual_socks_rejects_udp_and_domains(self):
        port = self.server()
        for request in (b"\x05\x03\x00\x01", b"\x05\x01\x00\x03"):
            with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                client.sendall(b"\x05\x01\x00")
                self.assertEqual(receive(client, 2), b"\x05\x00")
                client.sendall(request)
                self.assertEqual(receive(client, 10)[1], 7)

    def test_actual_tcp_relay_preserves_response_after_half_close(self):
        port = self.server()
        upstream, responder = socket.socketpair()
        def respond():
            with responder:
                self.assertEqual(responder.recv(4), b"ping")
                self.assertEqual(responder.recv(1), b"")
                responder.sendall(b"pong")
        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        with patch.object(guard, "open_target", return_value=upstream):
            with connect(port, "10.1.2.3", 22) as client:
                client.sendall(b"ping")
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(receive(client, 4), b"pong")
                self.assertEqual(client.recv(1), b"")
        thread.join(2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
