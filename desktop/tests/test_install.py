import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install_tools as installer
import service


class InstallTests(unittest.TestCase):
    def test_user_setup_never_writes_as_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            victim = root / 'victim'
            victim.write_text('original')
            agents = root / 'Library/LaunchAgents'
            agents.mkdir(parents=True)
            (agents / 'test.plist').symlink_to(victim)
            with patch.object(installer.os, 'geteuid', return_value=0):
                with self.assertRaises(RuntimeError):
                    installer.write_user_plist(root, 'test', {'Label': 'test'})
            self.assertEqual(victim.read_text(), 'original')

    def test_user_plist_replaces_symlink_without_overwriting_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            victim = root / 'victim'
            victim.write_text('original')
            agents = root / 'Library/LaunchAgents'
            agents.mkdir(parents=True)
            link = agents / 'test.plist'
            link.symlink_to(victim)
            with patch.object(installer.os, 'geteuid', return_value=501):
                installer.write_user_plist(root, 'test', {'Label': 'test'})
            self.assertEqual(victim.read_text(), 'original')
            self.assertFalse(link.is_symlink())
            self.assertEqual(plistlib.loads(link.read_bytes()), {'Label': 'test'})

    def test_stop_waits_for_process_even_after_registration_disappears(self):
        with patch.object(installer, 'job_info', side_effect=['pid = 123\n', None, None]), \
             patch.object(installer, 'run'), \
             patch.object(installer.os, 'kill', side_effect=[None, ProcessLookupError()]), \
             patch.object(installer.time, 'sleep') as sleep:
            installer.stop_job('system/test')
        sleep.assert_called_once()

    def test_stop_waits_for_registration_after_process_exits(self):
        with patch.object(installer, 'job_info', side_effect=['pid = 123\n', 'state = terminating', None]), \
             patch.object(installer, 'run'), \
             patch.object(installer.os, 'kill', side_effect=ProcessLookupError()), \
             patch.object(installer.time, 'sleep') as sleep:
            installer.stop_job('system/test')
        sleep.assert_called_once()

    def test_stop_timeout_does_not_force_kill(self):
        with patch.object(installer, 'job_info', return_value='pid = 123\n'), \
             patch.object(installer, 'run', return_value=Mock(stderr='')), \
             patch.object(installer.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, '未完全退出'):
                installer.stop_job('system/test', timeout=0)
        kill.assert_called_once_with(123, 0)

    def test_query_permission_error_is_not_treated_as_stopped(self):
        result = subprocess.CompletedProcess([], 1, '', 'Operation not permitted')
        with patch.object(installer, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, '无法查询'):
                installer.job_info('system/test')

    def test_bootstrap_recovers_from_removal_race(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.plist'
            path.write_bytes(plistlib.dumps({'Label': 'test'}))
            results = [subprocess.CompletedProcess([], 5, '', 'Input/output error'),
                       subprocess.CompletedProcess([], 0, '', '')]
            with patch.object(installer, 'run', side_effect=results) as run, \
                 patch.object(installer, 'job_info', side_effect=[None, f'path = {path}\n']), \
                 patch.object(installer.time, 'sleep'):
                installer.start_job('gui/501', path)
            self.assertEqual(run.call_count, 2)

    def test_bootstrap_persistent_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.plist'
            path.write_bytes(plistlib.dumps({'Label': 'test'}))
            with patch.object(installer, 'run', return_value=Mock(returncode=5, stderr='Input/output error')), \
                 patch.object(installer, 'job_info', return_value=None):
                with self.assertRaisesRegex(RuntimeError, '无法注册'):
                    installer.start_job('gui/501', path, timeout=0)

    def test_worker_check_rejects_stale_status(self):
        responses = [dict(ok=True, status=dict(version='old', pid=123, phase='active')),
                     dict(ok=True, status=dict(version='new', pid=123, phase='active'))]
        with patch.object(service, 'control', side_effect=responses), \
             patch.object(installer, 'job_info', return_value='pid = 123\n'), \
             patch.object(installer.time, 'sleep') as sleep:
            installer.wait_worker('new')
        sleep.assert_called_once()

    def test_failed_upgrade_restores_bundle_and_original_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app, backup = root / 'app', root / 'backup.app'
            app.mkdir()
            backup.mkdir()
            (app / 'version').write_text('new')
            (backup / 'version').write_text('old')
            daemon, config = root / 'daemon.plist', root / 'config.json'
            daemon.write_bytes(plistlib.dumps({'Label': 'test'}))
            config.write_text('original')
            snapshot = installer.snapshot_files([daemon, config])
            config.write_text('new config')
            with patch.object(installer, 'APP_TARGET', app), \
                 patch.object(installer, 'DAEMON', daemon), \
                 patch.object(installer.core, 'STATE', root / 'absent-state'), \
                 patch.object(installer, 'stop_job'), \
                 patch.object(installer, 'start_job') as start, \
                 patch.object(installer.os, 'chown'):
                installer.recover_install(backup, snapshot, os.getuid(), root)
            self.assertEqual((app / 'version').read_text(), 'old')
            self.assertEqual(config.read_text(), 'original')
            self.assertEqual(len(list(root.glob('*-failed-*.app'))), 1)
            start.assert_called_once_with('system', daemon)


if __name__ == '__main__':
    unittest.main()
