import copy
import json
import os
from pathlib import Path
import queue
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import router_core as core
import service


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        # Unit tests must never call the running machine's Clash API.
        overlay = patch.object(service.proxy_routing, 'Overlay')
        overlay.start()
        bridge = patch.object(service.transparent_route, 'Bridge')
        bridge.start()
        self.addCleanup(bridge.stop)
        self.addCleanup(overlay.stop)
        self.config = {'owner_uid': 501, 'profiles': copy.deepcopy(service.DEFAULT_PROFILES)}
        self.a, self.b = self.config['profiles']
        self.active = dict(name='utun7', index=29, gateway='vpn.a.example.org',
                           address='10.101.0.2', boot='boot-1')

    def test_b_both_gateways_and_no_a_ip(self):
        for gateway in ('vpn.b.example.org', 'vpn-alt.b.example.org'):
            profile = service.selected_profile(self.config['profiles'], gateway)
            self.assertEqual(profile['id'], 'b')
            self.assertEqual(profile['fixed_ips'], [])
            payload = core.clash_payload({'dns': {}, 'rules': []}, profile)
            self.assertIn('+.b.example.org', payload['dns']['nameserver-policy'])
            self.assertNotIn('+.a.example.org', payload['dns']['nameserver-policy'])
            self.assertFalse(any('10.101.0.10' in r for r in payload['rules']))

    def test_unknown_or_mismatched_gateway_never_gets_rules(self):
        self.assertIsNone(service.selected_profile(self.config['profiles'], 'vpn.example.com'))
        self.assertIsNone(service.selected_profile(self.config['profiles'], 'vpn.b.example.org', 'a'))

    def test_profile_validation(self):
        service.validate_config(self.config)
        self.config['profiles'][0]['domains'] = ['../../etc/hosts']
        with self.assertRaises(ValueError):
            service.validate_config(self.config)

    def test_restore_after_gateway_switch_does_not_recreate_old_routes(self):
        new = dict(self.active, gateway='vpn.b.example.org')
        state = {'removed': [{'net': '0.0.0.0/1', 'tunnel': self.active}],
                 'added': [{'net': '10.101.0.10/32', 'tunnel': self.active}]}
        calls = []
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(core, 'STATE', Path(temp)/'state'), \
             patch.object(core, 'tunnel', return_value=new), \
             patch.object(core, 'owned_host_route', return_value=True), \
             patch.object(core, 'command', side_effect=lambda a, *args: calls.append(a) or Mock(returncode=0)):
            core.restore(state)
        route_calls = [c for c in calls if c[0] == '/sbin/route']
        self.assertEqual(route_calls, [core.route_args('delete', '10.101.0.10/32', 'utun7')])

    def test_reboot_does_not_replay_stale_routes(self):
        new = dict(self.active, boot='boot-2')
        state = {'removed': [{'net': '0.0.0.0/1', 'tunnel': self.active}],
                 'added': [{'net': '10.101.0.10/32', 'tunnel': self.active}]}
        calls = []
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(core, 'STATE', Path(temp)/'state'), \
             patch.object(core, 'tunnel', return_value=new), \
             patch.object(core, 'owned_host_route', return_value=True), \
             patch.object(core, 'command', side_effect=lambda a, *args: calls.append(a)):
            core.restore(state)
        self.assertFalse(any(c[0] == '/sbin/route' for c in calls))

    def test_untagged_route_is_not_deleted_on_restore(self):
        state = {'added': [{'net': '10.101.0.10/32', 'tunnel': self.active}]}
        calls = []
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(core, 'STATE', Path(temp)/'state'), \
             patch.object(core, 'tunnel', return_value=self.active), \
             patch.object(core, 'owned_host_route', return_value=False), \
             patch.object(core, 'command', side_effect=lambda a, *args: calls.append(a)):
            core.restore(state)
        self.assertFalse(any(c[0] == '/sbin/route' for c in calls))

    def test_restored_native_routes_can_be_pruned_again(self):
        row = {'net': '192.0.2.0/24', 'iface': 'utun7', 'gateway': 'utun7', 'flags': 'USc'}
        self.assertTrue(core.removable(row, 'utun7', set()))
        self.assertFalse(core.removable(dict(row, flags='UHS1'), 'utun7', set()))

    def make_worker(self, temp):
        with patch.object(service, 'PREFERENCES', Path(temp)/'prefs'):
            worker = service.Worker(self.config)
        worker.preferences['enabled'] = True
        worker.publish = Mock()
        worker.cleanup = Mock(side_effect=lambda: setattr(worker, 'split', None))
        return worker

    def test_pause_restores_and_does_not_activate(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = self.make_worker(temp)
            worker.preferences['enabled'] = False
            worker.activate = Mock()
            worker.tick()
            worker.cleanup.assert_called_once()
            worker.activate.assert_not_called()
            self.assertEqual(worker.publish.call_args.args[0], 'paused')

    def test_disconnect_waits_without_exiting_service(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(core, 'gateway_host', return_value='vpn.a.example.org'), \
             patch.object(core, 'tunnel', side_effect=RuntimeError('disconnected')):
            worker = self.make_worker(temp)
            worker.tick()
            self.assertFalse(worker.stop.is_set())
            self.assertEqual(worker.publish.call_args.args[0], 'logged-out')

    def test_dns_precheck_timeout_after_wake_waits_quietly(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(core, 'gateway_host', return_value='vpn.a.example.org'), \
             patch.object(core, 'tunnel', return_value=self.active):
            worker = self.make_worker(temp)
            worker.activate = Mock(side_effect=TimeoutError('timed out'))
            with self.assertNoLogs(level='ERROR'):
                worker.tick()
            self.assertEqual(worker.publish.call_args.args[0], 'waiting')
            self.assertEqual(worker.failures, 1)
            self.assertGreater(worker.retry_at, 0)
            worker.tick()
            self.assertEqual(worker.activate.call_count, 1)

    def test_gateway_switch_cleans_old_session_and_waits(self):
        new = dict(self.active, gateway='vpn.b.example.org')
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(core, 'gateway_host', return_value='vpn.b.example.org'), \
             patch.object(core, 'tunnel', return_value=new):
            worker = self.make_worker(temp)
            worker.split = Mock(active=self.active, profile=self.a, accepting=True)
            worker.activate = Mock()
            worker.tick()
            worker.cleanup.assert_called_once()
            worker.activate.assert_not_called()
            self.assertEqual(worker.publish.call_args.args[0], 'waiting')

    def test_line_switch_on_same_utun_keeps_split(self):
        moved = dict(self.active, address='10.101.0.3')
        with patch.object(core, 'tunnel', return_value=self.active):
            split = core.Split(self.a)
        split.state = {'removed': [{'net': '0.0.0.0/1', 'tunnel': self.active}],
                       'added': [{'net': '10.101.0.10/32', 'tunnel': self.active}]}
        with patch.object(core, 'save') as save:
            self.assertTrue(split.retarget(moved))
        save.assert_called_once()
        self.assertEqual(split.active, moved)
        self.assertTrue(all(r['tunnel'] == moved for r in split.state['removed'] + split.state['added']))
        for change in ({'index': 30}, {'name': 'utun8'}, {'boot': 'boot-2'}, {'gateway': 'vpn.b.example.org'}):
            self.assertFalse(split.retarget(dict(moved, **change)))
        self.assertEqual(split.active, moved)

    def test_worker_follows_line_switch_without_cleanup(self):
        moved = dict(self.active, address='10.101.0.3')
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(core, 'gateway_host', return_value='vpn.a.example.org'), \
             patch.object(core, 'tunnel', return_value=moved):
            worker = self.make_worker(temp)
            split = Mock(active=self.active, profile=self.a, accepting=True, state={'added': []})
            split.retarget.side_effect = lambda new: setattr(split, 'active', new) or True
            worker.split = split
            worker.activate = Mock()
            worker.last_health = 1e18   # skip the DNS probe in this unit test
            with patch.object(service.time, 'monotonic', return_value=0):
                worker.tick()
            worker.cleanup.assert_not_called()
            worker.activate.assert_not_called()
            split.reconcile.assert_called_once()
            self.assertEqual(worker.last_health, 0)

    def test_clash_restart_reapplies_payload(self):
        with patch.object(core, 'tunnel', return_value=self.active):
            split = core.Split(self.a)
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)/'clash.yaml'
            config.write_text('dns: {}\nrules: []\n')
            sock = Path(temp)/'socket'
            sock.write_text('first')
            with patch.object(core, 'CLASH_CONFIG', config), patch.object(core, 'CLASH_SOCKET', str(sock)), \
                 patch.object(core, 'save'), patch.object(core, 'clash_request') as request:
                split.patch_clash()
                split.patch_clash()
                self.assertEqual(request.call_count, 1)
                replacement = Path(temp)/'other'
                replacement.write_text('new inode')
                replacement.replace(sock)
                split.patch_clash()
                self.assertEqual(request.call_count, 2)

    def test_listener_failure_never_commits_partial_split(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = self.make_worker(temp)
            split = Mock(active=self.active, state={'removed': [], 'added': []},
                         lock=threading.RLock())
            udp = Mock()
            with patch.object(core, 'Split', return_value=split), \
                 patch.object(core, 'upstream', return_value=b'answer'), \
                 patch.object(core, 'related_addresses', return_value=({'10.101.0.10'}, set())), \
                 patch.object(core, 'UDP', return_value=udp), \
                 patch.object(core, 'TCP', side_effect=OSError('port busy')), \
                 patch.object(core, 'restore') as restore:
                with self.assertRaises(RuntimeError):
                    worker.activate(self.a)
                restore.assert_called_once_with(split.state)
            self.assertIsNone(worker.split)
            self.assertEqual(worker.servers, [])
            udp.server_close.assert_called_once()
            split.reconcile.assert_not_called()
            split.setup_resolver.assert_not_called()

    def test_resolver_failure_rolls_back_routes_and_both_listeners(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = self.make_worker(temp)
            split = Mock(active=self.active, state={'added': ['journaled']},
                         lock=threading.RLock())
            split.setup_resolver.side_effect = OSError('resolver unavailable')
            udp, tcp = Mock(), Mock()
            with patch.object(core, 'Split', return_value=split), \
                 patch.object(core, 'upstream', return_value=b'answer'), \
                 patch.object(core, 'related_addresses', return_value=({'10.101.0.10'}, set())), \
                 patch.object(core, 'UDP', return_value=udp), \
                 patch.object(core, 'TCP', return_value=tcp), \
                 patch.object(service.threading, 'Thread'), \
                 patch.object(core, 'restore') as restore:
                with self.assertRaises(RuntimeError):
                    worker.activate(self.a)
                restore.assert_called_once_with(split.state)
            self.assertIsNone(worker.split)
            for listener in (udp, tcp):
                listener.shutdown.assert_called_once()
                listener.server_close.assert_called_once()

    def test_line_address_change_restores_covering_routes_before_hosts(self):
        moved = dict(self.active, address='10.101.0.3')
        state = {'removed': [{'net': '10.100.0.0/15', 'tunnel': self.active}],
                 'added': [{'net': '10.101.0.10/32', 'tunnel': self.active}]}
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(core, 'STATE', Path(temp)/'journal'), \
             patch.object(core, 'tunnel', return_value=moved), \
             patch.object(core, 'owned_host_route', return_value=True), \
             patch.object(core, 'command', return_value=Mock(returncode=0, stderr='')) as command:
            core.STATE.write_text(json.dumps(state))
            core.restore(state)
            route_calls = [c.args[0] for c in command.call_args_list if c.args[0][0] == '/sbin/route']
            self.assertEqual(route_calls, [core.route_args('add', '10.100.0.0/15', 'utun7'),
                                           core.route_args('delete', '10.101.0.10/32', 'utun7')])
            self.assertFalse(core.STATE.exists())

    def test_restore_failure_retains_journal_and_owned_host(self):
        state = {'removed': [{'net': '10.100.0.0/15', 'tunnel': self.active}],
                 'added': [{'net': '10.101.0.10/32', 'tunnel': self.active}]}
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(core, 'STATE', Path(temp)/'journal'), \
             patch.object(core, 'tunnel', return_value=self.active), \
             patch.object(core, 'command', return_value=Mock(returncode=1, stderr='failed')) as command:
            core.STATE.write_text(json.dumps(state))
            with self.assertRaises(RuntimeError):
                core.restore(state)
            self.assertEqual(json.loads(core.STATE.read_text()), state)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0][2], 'add')

    def test_disable_ack_waits_until_overlay_restore_succeeds(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(service, 'STATUS', Path(temp)/'status'), \
             patch.object(service, 'PREFERENCES', Path(temp)/'prefs'):
            worker = self.make_worker(temp)
            worker.status = {'phase': 'paused'}
            request = {'command': 'connections', 'native': None, 'container': None, 'revision': 3}
            worker.requests.put(request)
            worker.overlay.apply.side_effect = RuntimeError('restore failed')
            worker.tick()
            self.assertIsNone(worker.status['assignment'])
            self.assertFalse(worker.status['routes_restored'])
            worker.overlay.apply.side_effect = None
            worker.overlay.apply.return_value = False
            worker.tick()
            self.assertEqual(worker.status['assignment'], {'native': None, 'container': None, 'revision': 3})
            self.assertTrue(worker.status['routes_restored'])


class ControllerTests(unittest.TestCase):
    def test_narrow_control_api(self):
        class Stub:
            requests = queue.Queue()
            wake = threading.Event()
            status = {'phase': 'waiting'}
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp)/'socket')
            server = service.ControlServer(path, service.ControlHandler)
            server.worker = Stub()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(service, 'SOCKET', Path(path)):
                    self.assertTrue(service.control({'command': 'pause'})['ok'])
                    self.assertFalse(service.control({'command': 'exec', 'cmd': 'whoami'})['ok'])
                    self.assertFalse(service.control({'command': 'profile', 'profile': '../bad'})['ok'])
                self.assertEqual(server.worker.requests.qsize(), 1)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
