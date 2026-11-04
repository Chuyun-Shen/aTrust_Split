import copy
import unittest

import profiles


class ProfileTests(unittest.TestCase):
    def test_reserved_examples_cannot_drive_live_connections(self):
        with self.assertRaises(ValueError):
            profiles.require_configured(copy.deepcopy(profiles.EXAMPLE_PROFILES))

    def test_overlapping_institution_scopes_are_rejected(self):
        values = copy.deepcopy(profiles.EXAMPLE_PROFILES)
        values[1]['domains'] = ['sub.a.example.org']
        with self.assertRaises(ValueError):
            profiles.validate_profiles(values)

    def test_probe_cannot_target_unrelated_address_or_gateway(self):
        for target in ('outside.example.net', 'vpn.a.example.org', '10.101.0.99'):
            values = copy.deepcopy(profiles.EXAMPLE_PROFILES)
            values[0]['business_probe']['host'] = target
            with self.assertRaises(ValueError):
                profiles.validate_profiles(values)

    def test_login_url_cannot_embed_credentials_or_switch_institution(self):
        for url in ('https://name:secret@vpn.a.example.org/portal/', 'https://vpn.b.example.org/portal/'):
            values = copy.deepcopy(profiles.EXAMPLE_PROFILES)
            values[0]['login_url'] = url
            with self.assertRaises(ValueError):
                profiles.validate_profiles(values)


if __name__ == '__main__':
    unittest.main()
