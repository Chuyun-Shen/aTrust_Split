from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from atrust_split.config import load
from atrust_split import routing

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = {"name": "utun7", "index": 17, "address": "172.20.0.2", "boot": "boot-a",
            "gateway": "vpn-a.example.org", "kind": "native"}


def route(network="10.0.0.0/8", interface="utun7", flags="US", gateway="utun7"):
    return {"net": network, "iface": interface, "flags": flags, "gateway": gateway}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = routing.Journal(Path(self.directory.name))

    def test_route_parser_handles_macos_shorthand(self):
        parsed = routing.parse_routes("10 utun7 US utun7\n10.2.3.4 utun7 UHS1 utun7\ndefault 192.0.2.1 UGSc en0\n")
        self.assertEqual([entry["net"] for entry in parsed], ["10.0.0.0/8", "10.2.3.4/32", "0.0.0.0/0"])

    def test_native_identity_requires_vendor_signature_and_expected_gateway(self):
        output = "utun7: flags=UP\n inet 172.20.0.2 --> 2.0.0.1 netmask 0xffffffff\n inet6 fd53:414e:4746:4f52:1\n"
        args = (output, "vpn-a.example.org", ["vpn-a.example.org"], "boot-a")
        self.assertEqual(routing.native_identity(*args, index=lambda name: 17), IDENTITY)
        self.assertIsNone(routing.native_identity(output, "another.example.org", args[2], "boot-a"))
        self.assertIsNone(routing.native_identity(output.replace("fd53", "fd00"), *args[1:]))

    def test_only_vendor_owned_broad_routes_are_removable(self):
        self.assertTrue(routing.removable(route(), IDENTITY, set()))
        for entry in (route(interface="utun8"), route(flags="USI"), route(flags="UHS1"),
                      route(flags="UHL"), route(gateway="192.0.2.1")):
            self.assertFalse(routing.removable(entry, IDENTITY, set()))
        self.assertFalse(routing.removable(route(), IDENTITY, {"10.0.0.0/8"}))

    def test_new_target_journal_precedes_route_mutation(self):
        def command(arguments, check=True):
            self.assertEqual(self.journal.path.exists(), True)
            persisted = routing.Journal(self.journal.directory)
            persisted.load()
            self.assertEqual(persisted.value["added"][0]["net"], "10.1.2.3/32")
            self.assertEqual(arguments, ["/sbin/route", "-n", "add", "-host", "10.1.2.3", "-interface", "utun7", "-proto1"])
            return subprocess.CompletedProcess(arguments, 0)
        with patch.object(routing, "routes", return_value=[]), patch.object(routing, "run", side_effect=command):
            routing.add_target(self.journal, IDENTITY, "10.1.2.3")

    def test_existing_exact_routes_are_never_claimed(self):
        with patch.object(routing, "routes", return_value=[route("10.1.2.3/32")]), patch.object(routing, "run") as command:
            routing.add_target(self.journal, IDENTITY, "10.1.2.3")
        self.assertEqual(self.journal.value["added"], [])
        command.assert_not_called()

    def test_conflicting_exact_route_is_not_overwritten(self):
        with patch.object(routing, "routes", return_value=[route("10.1.2.3/32", interface="utun9")]), patch.object(routing, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "another interface"):
                routing.add_target(self.journal, IDENTITY, "10.1.2.3")
        command.assert_not_called()

    def test_failed_add_does_not_claim_route(self):
        with patch.object(routing, "routes", return_value=[]), patch.object(routing, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(RuntimeError):
                routing.add_target(self.journal, IDENTITY, "10.1.2.3")
        self.assertEqual(self.journal.value["added"], [])

    def test_restore_broad_routes_before_removing_added_routes(self):
        self.journal.value["removed"] = [{"net": "10.0.0.0/8", "identity": IDENTITY}]
        self.journal.value["added"] = [{"net": "10.1.2.3/32", "identity": IDENTITY}]
        with patch.object(routing, "identity_alive", return_value=True), \
             patch.object(routing, "routes", side_effect=[[], [route("10.1.2.3/32", flags="UHS1")]]), \
             patch.object(routing, "run", return_value=subprocess.CompletedProcess([], 0)) as command:
            routing.restore(self.journal, "boot-a")
        self.assertEqual([call.args[0][2] for call in command.call_args_list], ["add", "delete"])

    def test_restore_failure_preserves_journal_and_target_routes(self):
        self.journal.value["removed"] = [{"net": "10.0.0.0/8", "identity": IDENTITY}]
        self.journal.value["added"] = [{"net": "10.1.2.3/32", "identity": IDENTITY}]
        with patch.object(routing, "identity_alive", return_value=True), patch.object(routing, "routes", return_value=[]), \
             patch.object(routing, "run", return_value=subprocess.CompletedProcess([], 1)) as command:
            with self.assertRaisesRegex(RuntimeError, "journal retained"):
                routing.restore(self.journal, "boot-a")
        self.assertTrue(self.journal.path.exists())
        self.assertEqual(len(self.journal.value["added"]), 1)
        self.assertEqual(command.call_count, 1)

    def test_untagged_target_is_not_removed(self):
        self.journal.value["added"] = [{"net": "10.1.2.3/32", "identity": IDENTITY}]
        with patch.object(routing, "identity_alive", return_value=True), \
             patch.object(routing, "routes", return_value=[route("10.1.2.3/32", flags="UHS")]), \
             patch.object(routing, "run") as command:
            routing.restore(self.journal, "boot-a")
        command.assert_not_called()

    def test_reboot_does_not_replay_old_routes(self):
        self.journal.value["removed"] = [{"net": "10.0.0.0/8", "identity": IDENTITY}]
        with patch.object(routing, "run") as command:
            routing.restore(self.journal, "boot-b")
        command.assert_not_called()

    def test_reused_interface_index_does_not_match(self):
        with patch.object(routing.socket, "if_nametoindex", return_value=99), patch.object(routing, "run") as command:
            self.assertFalse(routing.identity_alive(IDENTITY, "boot-a"))
        command.assert_not_called()

    def test_tun2socks_binds_loopback_not_default_interface(self):
        config = load(ROOT / "config.dual.example.toml")
        command = routing.tun_command(config, "/example/tun2socks")
        self.assertEqual(command[command.index("--interface") + 1], "lo0")
        self.assertEqual(command[command.index("--proxy") + 1], "socks5://127.0.0.1:10821")

    def test_gateway_switch_does_not_restore_old_broad_routes_but_removes_owned_hosts(self):
        self.journal.value["removed"] = [{"net": "10.0.0.0/8", "identity": IDENTITY}]
        self.journal.value["added"] = [{"net": "10.1.2.3/32", "identity": IDENTITY}]
        def alive(identity, boot, check_gateway=True):
            return not check_gateway
        with patch.object(routing, "identity_alive", side_effect=alive), \
             patch.object(routing, "routes", return_value=[route("10.1.2.3/32", flags="UHS1")]), \
             patch.object(routing, "run", return_value=subprocess.CompletedProcess([], 0)) as command:
            routing.restore(self.journal, "boot-a")
        self.assertEqual([call.args[0][2] for call in command.call_args_list], ["delete"])

    def test_restore_conflicting_broad_route_is_not_overwritten(self):
        self.journal.value["removed"] = [{"net": "10.0.0.0/8", "identity": IDENTITY}]
        with patch.object(routing, "identity_alive", return_value=True), \
             patch.object(routing, "routes", return_value=[route(interface="utun9")]), patch.object(routing, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "journal retained"):
                routing.restore(self.journal, "boot-a")
        command.assert_not_called()
        self.assertEqual(len(self.journal.value["removed"]), 1)


if __name__ == "__main__":
    unittest.main()
