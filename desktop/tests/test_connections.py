from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import connections
import connection_manager as manager
import container_support
import native_client
import profiles
import proxy_routing


class ChoicesTests(unittest.TestCase):
    def test_missing_choices_default_to_no_connections(self):
        with tempfile.TemporaryDirectory() as root, patch.object(connections, 'PATH', Path(root) / 'missing.json'):
            self.assertEqual(connections.plan(connections.read()), (None, None))

    def test_all_selection_and_placement_combinations(self):
        for chosen in [[], ['a'], ['b'], ['a', 'b']]:
            for preferred in ['a', 'b']:
                native, remote = connections.plan(dict(version=1, enabled=chosen, native_preference=preferred))
                self.assertEqual(set(filter(None, [native, remote])), set(chosen))
                self.assertEqual(remote is not None, len(chosen) == 2)
                if len(chosen) == 1:
                    self.assertEqual(native, chosen[0])
                if len(chosen) == 2:
                    self.assertEqual(native, preferred)

    def test_file_edits_are_validated_before_actions(self):
        for chosen in [['unknown'], ['a', 'a'], 'a', [None], [[]], [{}], [True]]:
            with self.subTest(chosen=chosen), self.assertRaises(ValueError):
                connections.plan(dict(version=1, enabled=chosen, native_preference='a'))
        for revision in [-1, True, 1.5, '1', 2**63]:
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                connections.plan(dict(version=1, enabled=[], native_preference='a', revision=revision))
        for preference in [None, [], {}, True, 'unknown']:
            with self.subTest(preference=preference), self.assertRaises(ValueError):
                connections.plan(dict(version=1, enabled=[], native_preference=preference))
        with self.assertRaises(ValueError):
            connections.plan(dict(version=True, enabled=[], native_preference='a'))

    def test_toggles_preserve_preference_and_other_selection(self):
        with tempfile.TemporaryDirectory() as root, patch.object(connections, 'PATH', Path(root) / 'connections.json'), \
             patch.object(connections, 'require_configured'):
            connections.update(school='a', enabled=True)
            connections.update(school='b', enabled=True)
            connections.update(native='b')
            result = connections.update(school='a', enabled=False)
            self.assertEqual(connections.plan(result), ('b', None))
            self.assertEqual(result['native_preference'], 'b')
            self.assertEqual(connections.PATH.stat().st_mode & 0o777, 0o600)

    def test_preference_only_change_does_not_create_new_deselection(self):
        with tempfile.TemporaryDirectory() as root, patch.object(connections, 'PATH', Path(root) / 'connections.json'), \
             patch.object(connections, 'require_configured'):
            connections.update(school='a', enabled=True)
            stopped = connections.update(school='a', enabled=False)
            preferred = connections.update(native='b')
            self.assertEqual(stopped['revision'], preferred['revision'])
            self.assertEqual(connections.plan(preferred), (None, None))

    def test_unconfigured_enable_does_not_save_selection(self):
        with tempfile.TemporaryDirectory() as root, patch.object(connections, 'PATH', Path(root) / 'connections.json'), \
             patch.object(connections, 'require_configured', side_effect=ValueError('configure profiles')):
            with self.assertRaises(ValueError):
                connections.update(school='a', enabled=True)
            self.assertFalse(connections.PATH.exists())
            self.assertEqual(connections.update(native='b')['enabled'], [])

    def test_malformed_update_parameters_fail_closed(self):
        with tempfile.TemporaryDirectory() as root, patch.object(connections, 'PATH', Path(root) / 'connections.json'):
            for kwargs in [{'school': ''}, {'school': []}, {'enabled': True}, {'native': ''}, {'native': []}]:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    connections.update(**kwargs)
            self.assertFalse(connections.PATH.exists())


class NativeTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(connections, 'require_configured')
        guard.start()
        self.addCleanup(guard.stop)

    def state(self, status, school='b'):
        return {'status': status, 'data': {'addr': connections.SCHOOLS[school]['gateway']}}

    def test_none_waits_for_actual_logout(self):
        with patch.object(native_client, 'request', side_effect=[self.state('online'), {}, self.state('online')]):
            with self.assertRaisesRegex(RuntimeError, '尚未确认断开'):
                native_client.reconcile(None)

    def test_offline_session_requires_logout_before_deselection(self):
        with patch.object(native_client, 'request', side_effect=[self.state('offline'), {}, self.state('logout')]) as req:
            self.assertEqual(native_client.reconcile(None)['phase'], 'stopped')
        self.assertEqual([c.args[0] for c in req.call_args_list], ['status', 'logout', 'status'])

    def test_swap_logs_out_old_school_without_claiming_new_login(self):
        with patch.object(native_client, 'request', side_effect=[self.state('online'), {}, self.state('logout')]) as req:
            result = native_client.reconcile('a')
        self.assertEqual(result['phase'], 'login-required')
        self.assertEqual([c.args[0] for c in req.call_args_list], ['status', 'logout', 'status'])

    def test_repeated_wait_for_user_does_not_keep_logging_out(self):
        with patch.object(native_client, 'request', return_value=self.state('logout')) as req:
            self.assertEqual(native_client.reconcile('a')['phase'], 'login-required')
        req.assert_called_once_with('status')

    def test_same_school_online_is_not_interrupted(self):
        with patch.object(native_client, 'request', return_value=self.state('online', 'a')) as req:
            self.assertEqual(native_client.reconcile('a')['phase'], 'online')
        req.assert_called_once_with('status')

    def test_stale_choice_cannot_logout_or_resume(self):
        for school, state in [(None, self.state('online')), ('a', self.state('online')), ('a', self.state('offline', 'a'))]:
            with self.subTest(school=school), patch.object(native_client, 'request', return_value=state) as req:
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    native_client.reconcile(school, still_current=Mock(side_effect=RuntimeError('changed')))
                req.assert_called_once_with('status')

    def test_unconfigured_choice_never_contacts_native_client(self):
        with patch.object(connections, 'require_configured', side_effect=ValueError('configure')), \
             patch.object(native_client, 'request') as req:
            with self.assertRaises(ValueError):
                native_client.reconcile('a')
            req.assert_not_called()


class ManagerTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(connections, 'require_configured')
        guard.start()
        self.addCleanup(guard.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        status_path = patch.object(connections, 'STATUS', Path(directory.name) / 'status.json')
        status_path.start()
        self.addCleanup(status_path.stop)

    def config(self, enabled, preferred='a', revision=1):
        return dict(version=1, enabled=enabled, native_preference=preferred, revision=revision)

    def prepared(self):
        obj = manager.Manager()
        obj.stop_vm = Mock()
        obj.vm_running = Mock(return_value=False)
        obj.start_remote = Mock()
        obj.configure_routing = Mock(return_value=True)
        return obj

    def test_single_selection_stops_vm_before_native(self):
        actions = []
        obj = self.prepared()
        obj.stop_vm.side_effect = lambda: actions.append('stop-vm')
        with patch.object(connections, 'read', return_value=self.config(['a'])), \
             patch.object(connections, 'atomic_json'), \
             patch.object(native_client, 'reconcile', side_effect=lambda key, **kw: actions.append(('native', key)) or {'phase': 'online'}):
            obj.tick()
        self.assertEqual(actions, ['stop-vm', ('native', 'a')])
        obj.configure_routing.assert_called_once_with('a', None, 1)
        obj.start_remote.assert_not_called()

    def test_restart_stops_actual_conflicting_container_before_native(self):
        obj = self.prepared()
        obj.vm_running.return_value = True
        actions = []
        with patch.object(connections, 'read', return_value=self.config(['a', 'b'])), \
             patch.object(connections, 'atomic_json'), \
             patch.object(container_support, 'stop_other', side_effect=lambda key: actions.append(('stop-other', key))), \
             patch.object(native_client, 'reconcile', side_effect=lambda key, **kw: actions.append(('native', key)) or {'phase': 'online'}):
            obj.tick()
        self.assertEqual(actions, [('stop-other', 'b'), ('native', 'a')])

    def test_failed_logout_does_not_start_duplicate_institution(self):
        obj = self.prepared()
        with patch.object(connections, 'read', return_value=self.config(['a', 'b'])), \
             patch.object(connections, 'atomic_json') as publish, \
             patch.object(native_client, 'reconcile', side_effect=RuntimeError('logout failed')):
            obj.tick()
        obj.start_remote.assert_not_called()
        self.assertEqual(publish.call_args.args[1]['phase'], 'error')

    def test_confirmed_deselection_stays_ready_when_native_goes_away(self):
        obj = self.prepared()
        with patch.object(connections, 'read', return_value=self.config([])), \
             patch.object(connections, 'atomic_json') as publish, \
             patch.object(native_client, 'reconcile', side_effect=[{'phase': 'stopped'}, OSError('unavailable')]) as native:
            obj.tick()
            obj.tick()
        self.assertEqual(native.call_count, 1)
        self.assertEqual(publish.call_args.args[1]['phase'], 'ready')

    def test_completed_deselection_survives_manager_restart(self):
        connections.atomic_json(connections.STATUS, {
            'phase': 'ready', 'revision': 1, 'native': None, 'container': None,
            'native_status': {'phase': 'stopped'}})
        obj = self.prepared()
        with patch.object(connections, 'read', return_value=self.config([])), \
             patch.object(connections, 'atomic_json') as publish, \
             patch.object(native_client, 'reconcile', side_effect=OSError('unavailable')) as native:
            obj.tick()
        native.assert_not_called()
        self.assertEqual(publish.call_args.args[1]['phase'], 'ready')

    def test_unconfirmed_deselection_reports_native_failure(self):
        obj = self.prepared()
        with patch.object(connections, 'read', return_value=self.config([])), \
             patch.object(connections, 'atomic_json') as publish, \
             patch.object(native_client, 'reconcile', side_effect=OSError('unavailable')):
            obj.tick()
        obj.stop_vm.assert_called_once()
        self.assertEqual(publish.call_args.args[1]['phase'], 'error')

    def test_selection_change_during_vm_stop_prevents_stale_native_action(self):
        obj = self.prepared()
        with patch.object(connections, 'read', side_effect=[self.config([]), self.config(['a'], revision=2)]), \
             patch.object(connections, 'atomic_json'), patch.object(native_client, 'reconcile') as native:
            with self.assertRaisesRegex(RuntimeError, '选择已更改'):
                obj.tick()
        native.assert_not_called()
        obj.configure_routing.assert_not_called()

    def test_unconfigured_enabled_choice_has_no_external_effect(self):
        obj = self.prepared()
        with patch.object(connections, 'read', return_value=self.config(['a'])), \
             patch.object(connections, 'require_configured', side_effect=ValueError('configure')), \
             patch.object(native_client, 'reconcile') as native:
            with self.assertRaises(ValueError):
                obj.tick()
        obj.stop_vm.assert_not_called()
        obj.vm_running.assert_not_called()
        obj.configure_routing.assert_not_called()
        native.assert_not_called()

    def test_missing_settings_default_empty_choice_has_no_external_effect(self):
        obj = self.prepared()
        with patch.object(connections, 'read', return_value=self.config([])), \
             patch.object(connections, 'require_configured', side_effect=ValueError('configure')), \
             patch.object(native_client, 'reconcile') as native:
            with self.assertRaises(ValueError):
                obj.tick()
        obj.stop_vm.assert_not_called()
        obj.configure_routing.assert_not_called()
        native.assert_not_called()

    def test_queue_acceptance_does_not_publish_ready(self):
        obj = self.prepared()
        obj.configure_routing.return_value = False
        with patch.object(connections, 'read', return_value=self.config([])), \
             patch.object(connections, 'atomic_json') as publish, \
             patch.object(native_client, 'reconcile', return_value={'phase': 'stopped'}):
            obj.tick()
        self.assertEqual(publish.call_args.args[1]['phase'], 'applying')

    def test_daemon_restart_retries_same_assignment(self):
        obj = manager.Manager()
        applied = {'native': 'b', 'container': None, 'revision': 1}
        with patch.object(manager.service, 'control', side_effect=[
            {'ok': True, 'status': {'assignment': applied}},
            {'ok': True, 'status': {}}, {'ok': True}]) as control:
            self.assertTrue(obj.configure_routing('b', None, 1))
            self.assertFalse(obj.configure_routing('b', None, 1))
        self.assertEqual(control.call_args.args[0], {'command': 'connections', **applied})

    def test_deselection_ack_requires_restored_routes(self):
        obj = manager.Manager()
        assignment = {'native': None, 'container': None, 'revision': 1}
        with patch.object(manager.service, 'control', return_value={'ok': True, 'status': {'assignment': assignment}}):
            self.assertFalse(obj.configure_routing(None, None, 1))

    def test_vm_stop_is_not_claimed_when_still_running(self):
        obj = manager.Manager()
        obj.vm_running = Mock(return_value=True)
        with patch.object(container_support, 'stop_other'), patch.object(manager.dual, 'run'):
            with self.assertRaisesRegex(RuntimeError, '尚未确认停止'):
                obj.stop_vm()
        self.assertFalse(obj.vm_stopped)

    def test_vm_inventory_failure_propagates(self):
        obj = manager.Manager()
        with patch.object(manager.dual, 'run', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                obj.vm_running()


class RoutingTests(unittest.TestCase):
    def test_clash_composes_both_directions_without_changing_input(self):
        original = {'dns': {}, 'rules': ['MATCH,Existing'], 'proxies': []}
        for native, remote in [('a', 'b'), ('b', 'a')]:
            profile = next(p for p in profiles.EXAMPLE_PROFILES if p['id'] == native)
            result = proxy_routing.payload(original, profile, remote)
            self.assertIn(f'DOMAIN-SUFFIX,{native}.example.org,DIRECT', result['rules'])
            self.assertIn(f'DOMAIN-SUFFIX,{remote}.example.org,ATrust-Container', result['rules'])
            self.assertEqual(result['proxies'][0]['port'], connections.SCHOOLS[remote]['socks_port'])
            self.assertEqual(result['rules'][-1], 'MATCH,Existing')
        self.assertEqual(original, {'dns': {}, 'rules': ['MATCH,Existing'], 'proxies': []})

    def test_overlay_can_serve_container_while_native_waits_for_login(self):
        result = proxy_routing.payload({'dns': {}, 'rules': []}, None, 'b')
        self.assertIn('DOMAIN-SUFFIX,b.example.org,ATrust-Container', result['rules'])
        self.assertNotIn('+.a.example.org', result['dns']['nameserver-policy'])


if __name__ == '__main__':
    unittest.main()
