import importlib.util
from pathlib import Path
import socket
import struct
import unittest
from unittest.mock import patch
import tempfile

spec = importlib.util.spec_from_file_location('split', Path(__file__).resolve().parents[1] / 'router_core.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
PROFILE = {'domains': ['a.example.org'], 'fixed_ips': ['10.101.0.10']}


def record(name, typ, payload):
    return name + struct.pack('!HHIH', typ, 1, 3600, len(payload)) + payload


class Tests(unittest.TestCase):
    def test_netstat_shorthand(self):
        self.assertEqual(m.normalize_v4('192.0.2/24'), '192.0.2.0/24')
        self.assertEqual(m.normalize_v4('10.101'), '10.101.0.0/16')
        self.assertEqual(m.normalize_v4('100.64.0.1', True), '100.64.0.1/32')
        self.assertEqual(m.normalize_v4('default'), '0.0.0.0/0')

    def test_only_atrust_gateway_routes_removed(self):
        r = dict(net='192.0.2.0/24', gateway='utun7', iface='utun7', flags='UGc')
        self.assertTrue(m.removable(r, 'utun7', set()))
        self.assertFalse(m.removable(r, 'utun7', {r['net']}))
        self.assertFalse(m.removable(dict(r, gateway='link#30'), 'utun7', set()))
        self.assertFalse(m.removable(dict(r, iface='utun9'), 'utun7', set()))
        self.assertFalse(m.removable(dict(r, flags='UGcI'), 'utun7', set()))

    def test_domain_boundary(self):
        for name in ['a.example.org', 'www.a.example.org', 'a.b.a.example.org']:
            self.assertTrue(m.campus(name, PROFILE))
        for name in ['evila.example.org', 'a.example.org.example.com', 'example.com']:
            self.assertFalse(m.campus(name, PROFILE))

    def test_query_roundtrip(self):
        q = m.make_query('www.a.example.org', 28)
        self.assertEqual(m.question(q)[:3], ('www.a.example.org', 28, 1))
        self.assertEqual(m.question(m.empty_response(q, 5)), m.question(q))
        self.assertEqual(m.empty_response(q, 5)[3] & 15, 5)

    def test_pointer_cycle_rejected(self):
        with self.assertRaises(ValueError):
            m.read_name(b'\xc0\x00', 0)

    def test_short_label_rejected(self):
        with self.assertRaises(ValueError):
            m.read_name(b'\x08hello', 0)

    def test_unrelated_addresses_not_routed(self):
        q = m.make_query('www.a.example.org')
        response = (q[:2] + struct.pack('!5H', 0x8180, 1, 1, 0, 1) + q[12:]
                    + record(b'\xc0\x0c', 1, socket.inet_aton('198.51.100.21'))
                    + record(b'\x04evil\x03com\0', 1, socket.inet_aton('192.0.2.4')))
        self.assertEqual(m.related_addresses(response)[0], {'198.51.100.21'})

    def test_cname_chain(self):
        q = m.make_query('www.a.example.org')
        alias = b'\x03cdn\x07example\x03com\0'
        response = (q[:2] + struct.pack('!5H', 0x8180, 1, 2, 0, 0) + q[12:]
                    + record(b'\xc0\x0c', 5, alias)
                    + record(alias, 1, socket.inet_aton('198.51.100.21')))
        self.assertEqual(m.related_addresses(response), ({'198.51.100.21'}, {'cdn.example.com'}))

    def test_ipv6(self):
        q = m.make_query('www.a.example.org', 28)
        response = (q[:2] + struct.pack('!5H', 0x8180, 1, 1, 0, 0) + q[12:]
                    + record(b'\xc0\x0c', 28, socket.inet_pton(socket.AF_INET6, '2001:db8::1')))
        self.assertEqual(m.related_addresses(response)[0], {'2001:db8::1'})
        self.assertIn('-inet6', m.route_args('add', '2001:db8::1/128', 'utun7'))

    def test_no_tailscale_or_loopback_route(self):
        for address in ['127.0.0.1', '::1', '100.64.0.1', 'fd7a:115c:a1e0::1', '169.254.1.1', '224.0.0.1']:
            self.assertFalse(m.eligible(address))
        self.assertTrue(m.eligible('10.101.0.10'))

    def test_clash_patch_preserves_unrelated_settings(self):
        source = {'dns': {'nameserver-policy': {'+.example.com': ['1.1.1.1']},
                          'fake-ip-filter': ['*.lan'], 'nameserver': ['8.8.8.8']},
                  'rules': ['DOMAIN,example.com,proxy'], 'proxies': [{'name': 'keep'}]}
        result = m.clash_payload(source, PROFILE)
        self.assertEqual(result['proxies'], source['proxies'])
        self.assertEqual(result['dns']['nameserver'], ['8.8.8.8'])
        self.assertEqual(result['dns']['nameserver-policy']['+.example.com'], ['1.1.1.1'])
        self.assertEqual(source['dns']['fake-ip-filter'], ['*.lan'])
        self.assertEqual(result['rules'][-1], source['rules'][0])

    def test_clash_ambiguous_mode_rejected(self):
        with self.assertRaises(RuntimeError):
            m.clash_payload({'dns': {'fake-ip-filter-mode': 'whitelist'}}, PROFILE)

    def test_rollback_restores_covering_routes_before_removing_hosts(self):
        active = {'name': 'utun7', 'index': 29}
        state = {'removed': [{'net': '10.100.0.0/15', 'tunnel': active}],
                 'added': [{'net': '10.101.0.10/32', 'tunnel': active}]}
        calls = []
        class Result:
            returncode = 0
            stderr = ''
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, 'STATE', Path(tmp)/'state.json'), patch.object(m, 'tunnel', return_value=active), patch.object(m, 'owned_host_route', return_value=True), \
                 patch.object(m, 'command', side_effect=lambda a, *args: calls.append(a) or Result()):
                m.restore(state)
        self.assertEqual(calls[0], m.route_args('add', '10.100.0.0/15', 'utun7'))
        self.assertEqual(calls[1], m.route_args('delete', '10.101.0.10/32', 'utun7'))

    def test_rollback_wont_touch_reused_interface(self):
        old = {'name': 'utun7', 'index': 29}
        state = {'removed': [{'net': '0.0.0.0/1', 'tunnel': old}],
                 'added': [{'net': '10.101.0.10/32', 'tunnel': old}]}
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(m, 'STATE', Path(tmp)/'state.json'), \
                 patch.object(m, 'tunnel', return_value={'name': 'utun7', 'index': 45}), \
                 patch.object(m, 'command', side_effect=lambda a, *args: calls.append(a)):
                m.restore(state)
        self.assertFalse(any(a[0] == '/sbin/route' for a in calls))


if __name__ == '__main__':
    unittest.main()
