import socket
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dual_runtime as dual
import router_core as core
import ssl
import container_support as support


class ProxyHealthTests(unittest.TestCase):
    def probe(self, port):
        school = dict(socks_port=port, probe={'host': '10.101.0.10', 'port': 22, 'protocol': 'ssh'})
        with patch.dict(support.connections.SCHOOLS, {'a': school}):
            return support.probe('a')

    def server(self, response, banner=b'SSH-2.0-test\r\n'):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        requests = []
        def handle():
            with listener:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(2)
                    requests.append(support.receive(conn, 3))
                    conn.sendall(b'\x05\x00')
                    requests.append(support.receive(conn, 10))
                    conn.sendall(response)
                    if response[1] == 0:
                        conn.sendall(banner)
        thread = threading.Thread(target=handle, daemon=True)
        thread.start()
        return port, thread, requests

    def test_real_protocol_verifies_target_and_ssh_banner(self):
        port, thread, requests = self.server(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x16')
        self.assertTrue(self.probe(port))
        thread.join(2)
        self.assertEqual(requests, [b'\x05\x01\x00', b'\x05\x01\x00\x01\x0a\x65\x00\x0a\x00\x16'])

    def test_proxy_listening_alone_does_not_mean_vpn_connected(self):
        port, thread, _ = self.server(b'\x05\x05\x00\x01')
        with self.assertRaises(ConnectionError):
            self.probe(port)
        thread.join(2)

    def test_web_server_response_cannot_pass_ssh_health_check(self):
        port, thread, _ = self.server(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x16', b'HTTP/1.1 200 OK\r\n')
        with self.assertRaises(ConnectionError):
            self.probe(port)
        thread.join(2)

    def test_tls_uses_verified_context_and_configured_hostname(self):
        school = dict(socks_port=10812, probe={'host': 'intranet.b.example.org',
                                             'port': 8443, 'protocol': 'tls'})
        context = MagicMock()
        with patch.dict(support.connections.SCHOOLS, {'b': school}), \
             patch.object(support, 'dns', return_value=b'answer'), \
             patch.object(support, 'connect') as connect, \
             patch.object(support.ssl, 'create_default_context', return_value=context) as verified, \
             patch.object(core, 'related_addresses', return_value=({'198.51.100.10'}, set())):
            self.assertTrue(support.probe('b'))
            verified.assert_called_once_with()
            connect.assert_called_once_with(10812, '198.51.100.10', 8443)
            context.wrap_socket.assert_called_once_with(connect.return_value.__enter__.return_value,
                                                        server_hostname='intranet.b.example.org')

    def test_untrusted_tls_certificate_cannot_pass_health_check(self):
        school = dict(socks_port=10812, probe={'host': '198.51.100.10', 'port': 443, 'protocol': 'tls'})
        context = Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError('untrusted certificate')
        with patch.dict(support.connections.SCHOOLS, {'b': school}), \
             patch.object(support, 'connect'), \
             patch.object(support.ssl, 'create_default_context', return_value=context):
            with self.assertRaises(ssl.SSLCertVerificationError):
                support.probe('b')

    def test_missing_business_dns_address_never_opens_proxy(self):
        school = dict(socks_port=10812, probe={'host': 'intranet.b.example.org',
                                             'port': 443, 'protocol': 'tls'})
        with patch.dict(support.connections.SCHOOLS, {'b': school}), \
             patch.object(support, 'dns', return_value=b'answer'), \
             patch.object(core, 'related_addresses', return_value=(set(), set())), \
             patch.object(support, 'connect') as connect:
            with self.assertRaises(ConnectionError):
                support.probe('b')
            connect.assert_not_called()


class SessionStateTests(unittest.TestCase):
    LOGGED_OUT = ('default via 192.0.2.1 dev eth0 table 2\n'
                  'local 10.101.0.2 dev utun7 table local proto kernel scope host src 10.101.0.2\n'
                  'default via 192.0.2.1 dev eth0\n')

    def setUp(self):
        # Never read or write the real supervisor status from tests.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        status = patch.object(dual, 'STATUS', Path(temp.name)/'status.json')
        status.start()
        self.addCleanup(status.stop)
        configured = patch.object(dual.profiles, 'require_configured')
        configured.start()
        self.addCleanup(configured.stop)
        self.original_school = dual.SCHOOL
        dual.use_school('a')
        self.addCleanup(dual.use_school, self.original_school)

    def test_utun_address_alone_is_not_a_session(self):
        self.assertFalse(dual.tunnel_routed(self.LOGGED_OUT))
        self.assertTrue(dual.tunnel_routed(self.LOGGED_OUT + '10.101.0.0/16 dev utun7 scope link\n'))

    def test_gateway_urls_are_parsed_for_both_institutions(self):
        for school, host in (('a', 'vpn.a.example.org'), ('b', 'vpn.b.example.org')):
            for address in (host, host + ':8443', 'https://' + host + ':8443/portal/'):
                with self.subTest(school=school, address=address):
                    self.assertTrue(support.gateway_matches(address, school))
            self.assertFalse(support.gateway_matches('https://' + host + '.invalid:443', school))
            self.assertFalse(support.gateway_matches('https://vpn.other.example.org', school))

    def test_wrong_gateway_refuses_routes_for_both_institutions(self):
        for school in ('a', 'b'):
            dual.use_school(school)
            with patch.object(dual, 'docker', return_value=Mock(stdout='https://vpn.other.example.org:443/')) as docker, \
                 patch.object(dual.socket, 'create_connection') as network:
                self.assertEqual(dual.session_state(), 'login-required')
                self.assertEqual(docker.call_count, 1)
                network.assert_not_called()

    def routes(self, text):
        def result(*args, **kwargs):
            return Mock(stdout='https://vpn.a.example.org:8443/portal/' if 'cat' in args else text)
        return patch.object(dual, 'docker', side_effect=result)

    def test_logged_out_with_network_requires_login(self):
        with self.routes(self.LOGGED_OUT) as docker, patch.object(dual.socket, 'create_connection'):
            self.assertEqual(dual.session_state(), 'login-required')
        self.assertIn('ip route replace unreachable 10.101.0.10/32', docker.call_args_list[-1].args[-1])

    def test_logged_out_without_network_is_offline(self):
        with self.routes(self.LOGGED_OUT), \
             patch.object(dual.socket, 'create_connection', side_effect=OSError('down')):
            self.assertEqual(dual.session_state(), 'offline')

    def test_supervisor_reports_login_required_after_repeated_failures(self):
        sup = dual.Supervisor()
        published = []
        sup.publish = lambda phase, *a, **k: published.append(phase)
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(dual, 'CONFIG', Path(temp)/'config.json'), \
             patch.object(dual, 'PREFERENCES', Path(temp)/'prefs.json'), \
             patch.object(dual, 'run', return_value=Mock(returncode=0)), \
             patch.object(dual, 'docker', return_value=Mock(returncode=0, stdout='true\n')), \
             patch.object(dual, 'session_state', return_value='login-required'), \
             patch.object(dual.container_support, 'probe') as probe:
            (Path(temp)/'config.json').write_text('{}')
            sup.tick()
            sup.last_probe = None
            sup.tick()
        self.assertEqual(published, ['checking', 'login-required'])
        probe.assert_not_called()

    def test_blackhole_is_removed_once_tunnel_is_up(self):
        routed = 'unreachable 10.101.0.10/32 metric 4000\n10.101.0.0/16 dev utun7\n'
        linked = 'ESTAB 0 0 192.0.2.2:44322 203.0.113.111:441 users:(("aTrustXtunnel-6",pid=366,fd=48))\n'
        self.assertFalse(dual.tunnel_routed('unreachable 10.101.0.10/32 metric 4000\n'))
        def fake(*args, **kwargs):
            return Mock(stdout='vpn.a.example.org' if 'cat' in args else linked if 'ss' in args else routed)
        with patch.object(dual, 'docker', side_effect=fake) as docker:
            self.assertEqual(dual.session_state(), 'tunnel-up')
        script = docker.call_args_list[-1].args[-1]
        blackhole = [c.args for c in docker.call_args_list if 'sh' in c.args][0]
        self.assertIn('ip route del unreachable 10.101.0.10/32', blackhole[-1])

    def test_restored_routes_without_live_tunnel_require_login(self):
        routed = '10.101.0.0/16 dev utun7\n10.102.0.0/16 dev utun7\n'
        dead = ('SYN-SENT 0 1 192.0.2.2:33034 203.0.113.82:441 '
                'users:(("aTrustXtunnel-6",pid=366,fd=53))\n'
                'LISTEN 0 4096 127.0.0.1:59344 0.0.0.0:* users:(("aTrustXtunnel-6",pid=366,fd=33))\n')
        self.assertFalse(dual.tunnel_linked(dead))
        def fake(*args, **kwargs):
            return Mock(stdout='vpn.a.example.org' if 'cat' in args else dead if 'ss' in args else routed)
        with patch.object(dual, 'docker', side_effect=fake) as docker:
            self.assertEqual(dual.session_state(), 'link-down')
        self.assertNotIn('unreachable', docker.call_args_list[-1].args[-1])

    def supervise(self, sup, sessions, started, ticks, probe_error=OSError('down')):
        published = []
        remaining = list(sessions)
        def session():
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]
        sup.publish = lambda phase, *a, **k: published.append(phase)
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(dual, 'CONFIG', Path(temp)/'config.json'), \
             patch.object(dual, 'PREFERENCES', Path(temp)/'prefs.json'), \
             patch.object(dual, 'run', return_value=Mock(returncode=0)), \
             patch.object(dual, 'docker', return_value=Mock(returncode=0, stdout='true ' + started + '\n')), \
             patch.object(dual, 'session_state', side_effect=session), \
             patch.object(dual.container_support, 'probe', side_effect=probe_error), \
             patch.object(dual, 'stop_browser', return_value=True):
            (Path(temp)/'config.json').write_text('{}')
            for _ in range(ticks):
                sup.last_probe = None
                sup.tick()
        return published

    def test_lost_lines_after_live_tunnel_are_reconnecting(self):
        sup = dual.Supervisor()
        # tunnel seen, probe fails -> session re-read as link-down, twice
        published = self.supervise(sup, ['tunnel-up', 'link-down'], 't1', 2)
        self.assertEqual(sup.session, 'reconnecting')
        self.assertEqual(published[-1], 'reconnecting')

    def test_link_down_after_container_restart_requires_login(self):
        sup = dual.Supervisor()
        self.supervise(sup, ['tunnel-up', 'link-down'], 't1', 1)
        published = self.supervise(sup, ['link-down'], 't2', 2)
        self.assertEqual(sup.session, 'login-required')
        self.assertEqual(published[-1], 'login-required')

    def test_idle_session_without_link_is_probed_awake(self):
        # aTrust only dials :441 when traffic arrives; the probe is that traffic.
        sup = dual.Supervisor()
        published = self.supervise(sup, ['link-down'], 't1', 1, probe_error=None)
        self.assertEqual(published, ['connected'])
        self.assertEqual(sup.linked_start, 't1')

    def test_link_history_survives_supervisor_restart(self):
        dual.STATUS.write_text('{"linked_start": "t1"}')
        sup = dual.Supervisor()
        self.supervise(sup, ['link-down'], 't1', 2)
        self.assertEqual(sup.session, 'reconnecting')

    def test_browser_closes_only_after_probe_confirms_tunnel(self):
        sup = dual.Supervisor()
        sup.publish = lambda phase, *a, **k: None
        stopped = []
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(dual, 'CONFIG', Path(temp)/'config.json'), \
             patch.object(dual, 'PREFERENCES', Path(temp)/'prefs.json'), \
             patch.object(dual, 'run', return_value=Mock(returncode=0)), \
             patch.object(dual, 'docker', return_value=Mock(returncode=0, stdout='true\n')), \
             patch.object(dual, 'session_state', return_value='tunnel-up'), \
             patch.object(dual.container_support, 'probe', side_effect=OSError('down')) as probe, \
             patch.object(dual, 'stop_browser', side_effect=lambda: stopped.append(1) or True):
            (Path(temp)/'config.json').write_text('{}')
            sup.tick()          # probe fails first
            probe.side_effect = None
            sup.failures = 0
            sup.last_probe = None
            sup.tick()          # connected -> browser closed once
            sup.last_probe = None
            sup.tick()          # stays connected -> do not repeat
        self.assertEqual(len(stopped), 1)

    def test_browser_cleanup_is_rearmed_after_logout(self):
        sup = dual.Supervisor()
        sup.publish = lambda phase, *a, **k: None
        stopped = []
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(dual, 'CONFIG', Path(temp)/'config.json'), \
             patch.object(dual, 'PREFERENCES', Path(temp)/'prefs.json'), \
             patch.object(dual, 'run', return_value=Mock(returncode=0)), \
             patch.object(dual, 'docker', return_value=Mock(returncode=0, stdout='true\n')), \
             patch.object(dual, 'session_state', return_value='login-required'), \
             patch.object(dual.container_support, 'probe') as probe, \
             patch.object(dual, 'stop_browser', side_effect=lambda: stopped.append(1) or True):
            (Path(temp)/'config.json').write_text('{}')
            sup.tick()
            sup.browser_closed = True   # previous session had closed it
            sup.last_probe = None
            sup.tick()
        self.assertFalse(sup.browser_closed)
        self.assertEqual(stopped, [])

    def test_previously_connected_wrong_gateway_does_not_probe(self):
        sup = dual.Supervisor()
        sup.connected = True
        published = self.supervise(sup, ['login-required'], 't1', 2)
        self.assertFalse(sup.connected)
        self.assertEqual(published[-1], 'login-required')

    def test_memory_reports_zero_when_unreadable(self):
        with patch.object(dual, 'docker', side_effect=RuntimeError('down')):
            self.assertEqual(dual.memory_mb(), 0)
        with patch.object(dual, 'docker', return_value=Mock(stdout='1420000000\n')):
            self.assertEqual(dual.memory_mb(), 1354)


if __name__ == '__main__':
    unittest.main()
