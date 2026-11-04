from copy import deepcopy
from pathlib import Path
import unittest

from atrust_split.config import clash_fragment, load, require_configured, validate

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = load(ROOT / "config.dual.example.toml")

    def test_sample_is_valid_but_not_runnable(self):
        with self.assertRaisesRegex(ValueError, "Replace the example"):
            require_configured(self.config)

    def test_overlapping_institution_addresses_are_rejected(self):
        self.config["container"]["targets"] = self.config["native"]["targets"]
        with self.assertRaisesRegex(ValueError, "unique"):
            validate(self.config)

    def test_broad_and_special_targets_are_rejected(self):
        for address in ("0.0.0.0/0", "10.0.0.0/8", "127.0.0.1", "100.100.0.1", "198.18.1.1", "::1", "0.2.3.4", "255.255.255.255"):
            with self.subTest(address=address):
                config = deepcopy(self.config)
                config["native"]["targets"] = [address]
                with self.assertRaises(ValueError):
                    validate(config)

    def test_probe_cannot_escape_allowlist(self):
        self.config["probe"]["target"] = "192.0.2.10"
        with self.assertRaisesRegex(ValueError, "probe"):
            validate(self.config)

    def test_misspelled_settings_do_not_silently_take_effect(self):
        self.config["routing"]["auto_route"] = True
        with self.assertRaisesRegex(ValueError, "fields"):
            validate(self.config)

    def test_port_collisions_and_shell_values_are_rejected(self):
        for field, value in (("socks_port", 80), ("socks_port", True), ("socks_port", 18091),
                             ("context", "--host=elsewhere"), ("image", "image; command"),
                             ("login_url", "https://user:password@vpn.example.org"),
                             ("login_url", "https://vpn.example.org\nPASSWORD=oops")):
            with self.subTest(field=field, value=value):
                config = deepcopy(self.config)
                config["container"][field] = value
                with self.assertRaises(ValueError):
                    validate(config)

    def test_fragment_only_contains_exact_bypasses(self):
        fragment = clash_fragment(self.config)
        self.assertEqual(set(fragment), {"rules", "tun"})
        self.assertEqual(fragment["rules"], ["IP-CIDR,10.101.0.10/32,DIRECT,no-resolve", "IP-CIDR,10.102.0.20/32,DIRECT,no-resolve"])
        self.assertNotIn("dns", fragment)
        self.assertNotIn("proxies", fragment)


if __name__ == "__main__":
    unittest.main()
