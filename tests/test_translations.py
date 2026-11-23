"""Translation consistency checks."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "polish_energy_price"
    / "translations"
)


class IssueTranslationTests(unittest.TestCase):
    def test_issues_match_across_languages_and_keep_placeholders(self) -> None:
        issues = {
            lang: json.loads((TRANSLATIONS / f"{lang}.json").read_text("utf-8"))[
                "issues"
            ]
            for lang in ("pl", "en")
        }
        self.assertEqual(
            {key: set(value) for key, value in issues["pl"].items()},
            {key: set(value) for key, value in issues["en"].items()},
        )
        for lang, items in issues.items():
            description = items["compensation_rcem_failed"]["description"]
            self.assertIn("{entry}", description, lang)
            self.assertIn("{reason}", description, lang)


if __name__ == "__main__":
    unittest.main()
