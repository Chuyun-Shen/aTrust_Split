import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import transparent_route as route


class TransparentRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name) / 'routes.json'
        for name, value in [('JOURNAL', self.journal)]:
            p = patch.object(route, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.bridge = route.Bridge()
        self.record = dict(name='utun210', index=50, boot='test-boot', pid=12345,
                           process_identity='known start and binary', targets=['10.101.0.10'])

    def test_targets_are_explicit_single_addresses(self):
        for address in ['10.101.0.0/16', '::1', '127.0.0.1', '169.254.1.1']:
            with self.assertRaises(ValueError):
                route.targets(dict(id='a', fixed_ips=[address]))
        self.assertEqual(route.targets(dict(id='a', fixed_ips=['10.101.0.10'] * 2)), ['10.101.0.10'])

    def test_existing_foreign_route_is_never_replaced(self):
        self.bridge.record = self.record
        with patch.object(route.core, 'routes_v4', return_value=[dict(net='10.101.0.10/32', flags='UHS')]), \
             patch.object(route.core, 'owned_host_route', return_value=False), \
             patch.object(route.core, 'command') as command:
            with self.assertRaises(RuntimeError):
                self.bridge.add('10.101.0.10')
            command.assert_not_called()

    def test_stop_does_not_delete_replaced_route_or_kill_reused_pid(self):
        self.journal.write_text(json.dumps(self.record))
        with patch.object(route, 'link_alive', return_value=True), \
             patch.object(route.core, 'owned_host_route', return_value=False), \
             patch.object(route, 'owned_process', return_value=False), \
             patch.object(route.core, 'command') as command, \
             patch.object(route.os, 'kill') as kill:
            self.bridge.stop()
            command.assert_not_called()
            kill.assert_not_called()
        self.assertFalse(self.journal.exists())

    def test_early_start_failure_still_terminates_child(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        binary = Path(self.temp.name) / 'tun2socks'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        with patch.object(route, 'BINARY', binary), \
             patch.object(route.core, 'STATE_DIR', Path(self.temp.name)), \
             patch.object(route.core, 'command', side_effect=[Mock(stdout='lo0: flags='), RuntimeError('ps failed')]), \
             patch.object(route.subprocess, 'Popen', return_value=process):
            with self.assertRaises(RuntimeError):
                self.bridge.start(dict(id='a'))
        process.terminate.assert_called_once()
        self.assertIsNone(self.bridge.process)
        self.assertIsNone(self.bridge.log)

    def test_recovery_requires_same_boot_and_process_identity(self):
        with patch.object(route.core, 'boot_identity', return_value='other-boot'), \
             patch.object(route.core, 'command') as command:
            self.assertFalse(route.owned_process(self.record))
            command.assert_not_called()
        with patch.object(route.core, 'boot_identity', return_value='test-boot'), \
             patch.object(route.core, 'command', return_value=Mock(stdout='unrelated process')):
            self.assertFalse(route.owned_process(self.record))

    def test_removed_target_is_restored_without_restarting_bridge(self):
        self.bridge.record = self.record
        self.bridge.school = 'a'
        self.bridge.process = Mock()
        self.bridge.process.poll.return_value = None
        with patch.object(route, 'link_alive', return_value=True), \
             patch.object(route.core, 'owned_host_route', return_value=True), \
             patch.object(route.core, 'command') as command, \
             patch.object(self.bridge, 'add') as add:
            self.bridge.sync(dict(id='a', fixed_ips=['10.101.0.11']))
            self.assertEqual(command.call_args.args[0][2], 'delete')
            add.assert_called_once_with('10.101.0.11')
            self.bridge.process.terminate.assert_not_called()

    def test_dead_child_restarts_and_reconciles_routes(self):
        self.bridge.record = self.record
        self.bridge.school = 'a'
        self.bridge.process = Mock()
        self.bridge.process.poll.return_value = 1
        with patch.object(self.bridge, 'stop') as stop, \
             patch.object(self.bridge, 'start') as start, \
             patch.object(route, 'link_alive', return_value=True), \
             patch.object(self.bridge, 'add') as add:
            profile = dict(id='a', fixed_ips=['10.101.0.10'])
            self.bridge.sync(profile)
            stop.assert_called_once()
            start.assert_called_once_with(profile)
            add.assert_called_once_with('10.101.0.10')


if __name__ == '__main__':
    unittest.main()
