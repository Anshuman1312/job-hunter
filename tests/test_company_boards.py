"""Offline tests for company career feeds and referral tagging."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from company_boards import _guard_experience, _split_title_location  # noqa: E402
from config_loader import AppConfig  # noqa: E402


class CompanyFeedTests(unittest.TestCase):
    def test_successfactors_title_and_location(self) -> None:
        title, location = _split_title_location(
            "Python Developer (Pune, IND-36, IN, 411014)"
        )
        self.assertEqual(title, "Python Developer")
        self.assertEqual(location, "Pune, India")

    def test_successfactors_india_only_suffix(self) -> None:
        title, location = _split_title_location("Data Engineer (IN)")
        self.assertEqual(title, "Data Engineer")
        self.assertEqual(location, "India")

    def test_senior_title_without_years_becomes_five_plus(self) -> None:
        self.assertEqual(_guard_experience("Senior Python Developer", None), "5+ years")
        self.assertEqual(_guard_experience("Python Developer", None), None)
        self.assertEqual(_guard_experience("Senior Python Developer", "3-5 years"), "3-5 years")


class ReferralTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig.load(PROJECT_ROOT / "config.json")

    def test_aliases_match_legal_names(self) -> None:
        self.assertEqual(self.config.referral_for("Tata Consultancy Services"), "TCS")
        self.assertEqual(self.config.referral_for("Persistent Systems Ltd"), "Persistent")
        self.assertEqual(self.config.referral_for("HCLTech"), "HCL")
        self.assertEqual(self.config.referral_for("Barclays Global Service Centre"), "Barclays")

    def test_unknown_company_has_no_referral(self) -> None:
        self.assertIsNone(self.config.referral_for("Random Startup Pvt Ltd"))
        self.assertIsNone(self.config.referral_for(""))


if __name__ == "__main__":
    unittest.main()
