from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from atrust_split import cli, routing
from atrust_split.config import clash_fragment, load, require_configured, validate

ROOT = Path(__file__).resolve().parents[1]


class NativeModeTests(unittest.TestCase):
    def setUp(self):
        self.config = load(ROOT / "config.example.toml")

    def test_default_configuration_needs_no_container_or_tun(self):
        self.assertEqual(set(self.config), {"version", "native", "routing"})
        self.assertEqual(set(self.config["routing"]), {"poll_seconds"})
        self.assertEqual(clash_fragment(self.config)["rules"],
                         ["IP-CIDR,10.101.0.10/32,DIRECT,no-resolve"])
        with self.assertRaisesRegex(ValueError, "Replace the example"):
            require_configured(self.config)
        self.config["native"]["gateway_hosts"] = ["vpn.institution.test"]
        require_configured(self.config)

    def test_native_configuration_rejects_ambiguous_or_unsafe_settings(self):
        changes = [lambda c: c.update(probe={"target": "10.101.0.10", "port": 22}),
                   lambda c: c["routing"].update(device="utun210"),
                   lambda c: c["routing"].update(poll_seconds=True),
                   lambda c: c["native"].update(targets=["10.0.0.0/8"]),
                   lambda c: c["native"].update(targets=["10.101.0.10", "10.101.0.10"]),
                   lambda c: c["native"].update(gateway_hosts=["10.101.0.10"])]
        for change in changes:
            config = deepcopy(self.config)
            change(config)
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate(config)

    def test_dual_configuration_still_requires_all_extension_fields(self):
        for section in ("probe", "container"):
            config = load(ROOT / "config.dual.example.toml")
            del config[section]
            with self.subTest(section=section), self.assertRaises(ValueError):
                validate(config)

    def test_native_doctor_and_plan_do_not_require_optional_tools(self):
        with patch.object(cli.shutil, "which", return_value=None) as which:
            report = cli.doctor(self.config)
        which.assert_not_called()
        self.assertEqual(report["mode"], "native")
        self.assertEqual(report["ports_listed_in_config"], [])
        self.assertNotIn("container", cli.plan(self.config))

    def test_extension_commands_fail_before_touching_docker(self):
        for command in ("build", "up", "down", "login", "status", "probe"):
            with self.subTest(command=command), patch.object(cli, "Docker") as docker:
                error = io.StringIO()
                with redirect_stderr(error):
                    code = cli.main(["--config", str(ROOT / "config.example.toml"), command])
                self.assertEqual(code, 1)
                self.assertIn("optional second aTrust", error.getvalue())
                docker.assert_not_called()

    def test_native_routes_cli_accepts_missing_tun2socks(self):
        self.config["native"]["gateway_hosts"] = ["vpn.institution.test"]
        with patch.object(cli, "load", return_value=self.config), \
             patch.object(cli.shutil, "which", return_value=None), \
             patch.object(routing, "run_router") as router:
            self.assertEqual(cli.main(["routes"]), 0)
        router.assert_called_once_with(self.config, None)

    def exercise_router(self, reconciliation_error=None, change_session=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            journal = routing.Journal(Path(directory))
            stack.enter_context(patch.object(routing, "lock", return_value=nullcontext()))
            stack.enter_context(patch.object(routing, "Journal", return_value=journal))
            command = stack.enter_context(patch.object(routing, "run", return_value=subprocess.CompletedProcess([], 1)))
            stack.enter_context(patch.object(routing, "boot_identity", return_value="boot-test"))
            identity = {"name": "utun7", "boot": "boot-test"}
            stack.enter_context(patch.object(routing, "current_native", side_effect=[identity, None] if change_session else None,
                                             return_value=identity))
            event = Mock()
            event.wait.return_value = not change_session
            stack.enter_context(patch.object(routing.threading, "Event", return_value=event))
            stack.enter_context(patch.object(routing.signal, "signal"))
            reconcile = stack.enter_context(patch.object(routing, "reconcile_native", side_effect=reconciliation_error))
            restore = stack.enter_context(patch.object(routing, "restore"))
            process = stack.enter_context(patch.object(routing.subprocess, "Popen"))
            network = stack.enter_context(patch.object(routing.socket, "create_connection"))
            with redirect_stdout(io.StringIO()):
                if reconciliation_error or change_session:
                    with self.assertRaises(RuntimeError):
                        routing.run_router(self.config)
                else:
                    routing.run_router(self.config)
            reconcile.assert_called_once_with(self.config, identity, journal)
            restore.assert_called_once_with(journal, "boot-test")
            process.assert_not_called()
            network.assert_not_called()
            self.assertEqual(command.call_args_list[0].args[0][0], "/bin/launchctl")
            self.assertEqual(command.call_count, 1)

    def test_native_route_lifecycle_restores_without_starting_optional_components(self):
        self.exercise_router()

    def test_native_reconciliation_failure_still_restores(self):
        self.exercise_router(reconciliation_error=RuntimeError("route failure"))

    def test_native_session_change_restores_and_exits(self):
        self.exercise_router(change_session=True)


if __name__ == "__main__":
    unittest.main()
