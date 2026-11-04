import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from atrust_split.config import load
from atrust_split import containers

ROOT = Path(__file__).resolve().parents[1]


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = load(ROOT / "config.dual.example.toml")
        with patch.object(containers.shutil, "which", return_value="/example/docker"):
            self.docker = containers.Docker(self.config, Path(self.directory.name) / "config.local.toml")

    def test_secret_is_generated_locally_private_and_stable(self):
        environment = self.docker.environment_file()
        first = environment.read_text()
        self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.docker.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.docker.environment_file().read_text(), first)
        self.assertIn("AI_TARGETS=10.102.0.20\n", first)

    def test_foreign_container_is_never_adopted(self):
        responses = [Mock(stdout="some-id"), Mock(stdout=json.dumps([{"Config": {"Labels": {}}}]))]
        with patch.object(self.docker, "run", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "another installation"):
                self.docker.inspect()

    def test_changed_config_requires_explicit_container_recreation(self):
        existing = {"Config": {"Labels": {containers.LABEL + ".config": "old"}}, "State": {"Running": True}}
        with patch.object(self.docker, "run", return_value=Mock(stdout='[{"Architecture":"arm64","Id":"sha256:example"}]')) as command, \
             patch.object(self.docker, "inspect", return_value=existing):
            with self.assertRaisesRegex(RuntimeError, "configuration/image changed"):
                self.docker.up()
        self.assertEqual(command.call_count, 1)

    def test_down_keeps_volumes(self):
        with patch.object(self.docker, "inspect", return_value={"present": True}), patch.object(self.docker, "run") as command:
            self.docker.down(remove=True)
        self.assertEqual([call.args for call in command.call_args_list], [("stop", "--time", "20", containers.NAME), ("rm", containers.NAME)])

    def test_every_docker_command_uses_explicit_context_and_no_shell(self):
        with patch.object(containers.os, "geteuid", return_value=501), \
             patch.object(containers.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as command:
            self.docker.run("info")
        self.assertEqual(command.call_args.args[0], ["/example/docker", "--context", "colima-atrust-split", "info"])
        self.assertNotIn("shell", command.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
