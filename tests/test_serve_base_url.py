"""#74: the address a service publishes must be the address it listens on.

``serve --port 8787`` used to bind 8787 and then advertise the default port at
``/health``, because ``base_url`` came only from ``DRIVER_BASE_URL`` or the
built-in constant. The Harness doctor believed the advertisement and reported
a healthy driver as unreachable.

Every case below is a pure function of (settings, host, port, env), so the
whole defect is testable without binding a socket. The env mapping is injected
rather than mutated, following ``config.load_settings``.

NOTE ON VERIFICATION: written under D-2026-11-13-004, which forbids running a
local test battery until the CircleCI rail exists. These tests have never been
executed; they ride a branch for the rail to run.
"""
import unittest

from driver_core.cli import _advertised_settings
from driver_core.config import Settings


class AdvertisedAddressTest(unittest.TestCase):
    def test_requested_port_becomes_the_advertised_address(self):
        derived = _advertised_settings(Settings(), "127.0.0.1", 8787, env={})
        self.assertEqual(derived.base_url, "http://127.0.0.1:8787")
        self.assertEqual(derived.port, 8787)
        self.assertEqual(derived.host, "127.0.0.1")

    def test_requested_host_becomes_the_advertised_address(self):
        derived = _advertised_settings(Settings(), "0.0.0.0", 8787, env={})
        self.assertEqual(derived.base_url, "http://0.0.0.0:8787")

    def test_an_explicit_base_url_wins_over_the_bound_port(self):
        # A service behind a proxy or tunnel is reached somewhere other than
        # where it binds, so an explicit declaration is never overridden.
        settings = Settings(base_url="https://driver.example.test")
        derived = _advertised_settings(
            settings, "127.0.0.1", 8787,
            env={"DRIVER_BASE_URL": "https://driver.example.test"})
        self.assertIs(derived, settings)

    def test_port_zero_derives_nothing(self):
        # "any free port" is unknown until the socket is bound, so the
        # announce line stays the authority for that run.
        settings = Settings()
        self.assertIs(_advertised_settings(settings, "127.0.0.1", 0, env={}),
                      settings)

    def test_the_defaults_are_reproduced_when_nothing_is_requested(self):
        settings = Settings()
        derived = _advertised_settings(settings, settings.host, settings.port,
                                       env={})
        self.assertEqual(derived.base_url,
                         f"http://{settings.host}:{settings.port}")

    def test_the_original_settings_are_never_mutated(self):
        settings = Settings()
        _advertised_settings(settings, "127.0.0.1", 8787, env={})
        self.assertEqual(settings, Settings())


if __name__ == "__main__":
    unittest.main()
