"""Web-mode selection for the on-chain enrichment pass."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import onchain_scraper


class WebModeTests(unittest.TestCase):
    def test_all_searches_every_wallet(self):
        self.assertTrue(onchain_scraper.should_search_address("all", None))
        self.assertTrue(
            onchain_scraper.should_search_address(
                "all",
                onchain_scraper.EnsRecord("alice.eth", "", "", ""),
            )
        )

    def test_pivots_skips_address_only(self):
        self.assertFalse(onchain_scraper.should_search_address("pivots", None))
        self.assertTrue(
            onchain_scraper.should_search_address(
                "pivots",
                onchain_scraper.EnsRecord("alice.eth", "", "", ""),
            )
        )

    def test_none_never_searches(self):
        self.assertFalse(onchain_scraper.should_search_address("none", None))
        self.assertFalse(
            onchain_scraper.should_search_address(
                "none",
                onchain_scraper.EnsRecord("alice.eth", "", "", ""),
            )
        )

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            onchain_scraper.should_search_address("bogus", None)

    def test_run_rejects_an_unknown_mode_before_reading_targets(self):
        self.assertEqual(onchain_scraper.run(limit=1, web="bogus"), 2)


if __name__ == "__main__":
    unittest.main()
