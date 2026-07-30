"""Tests for strict discovery and parsing of official annual tariffs."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "polish_energy_price"
    / "official.py"
)
SPEC = importlib.util.spec_from_file_location("polish_energy_official", MODULE_PATH)
assert SPEC and SPEC.loader
official = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = official
SPEC.loader.exec_module(official)


class OfficialTariffTests(unittest.TestCase):
    def test_discovers_text_extract_and_rejects_gross_price_document(self) -> None:
        page = """
        <a href="/stawki-brutto-2026.pdf">TAURON Dystrybucja stawki brutto 2026</a>
        <a href="/wyciag-2026.ashx">Wyciąg z Taryfy TAURON Dystrybucja dla grup G 2026</a>
        """
        result = official.discover_distribution_document(
            page, "https://www.tauron-dystrybucja.pl/dokumenty", "tauron", 2026
        )
        self.assertEqual(
            "https://www.tauron-dystrybucja.pl/wyciag-2026.ashx", result.url
        )

    def test_discovers_stoen_custom_element(self) -> None:
        page = """
        <eon-ui-link text="Taryfa dla dystrybucji" 
          href="/files/2027/stoen-taryfa-dystrybucji-2027.pdf"></eon-ui-link>
        """
        result = official.discover_distribution_document(
            page, "https://www.stoen.pl/strona/dokumenty", "stoen", 2027
        )
        self.assertIn("2027.pdf", result.url)

    def test_parses_rates_from_sources_for_household_billing(self) -> None:
        self.assertEqual(
            0.0073,
            official.oze_rate_from_page(
                "<tr><td>2026</td><td>7,30</td><td>61/2025</td></tr>", 2026
            ),
        )
        text = (
            """
        DZIENNIK USTAW 2025 poz. 1664
        w sprawie wysokości stawki opłaty kogeneracyjnej na rok 2026
        Wysokość stawki opłaty kogeneracyjnej na rok 2026 wynosi 3,00 zł/MWh.
        """
            + "x" * 600
        )
        with patch.object(official, "extract_pdf_text", return_value=text):
            self.assertEqual(
                0.003, official.parse_cogeneration_rate_pdf(b"ignored", 2026)
            )

    def test_resolves_cogeneration_regulation_from_eli(self) -> None:
        content = """{"items":[{"ELI":"DU/2025/1664","title":
          "Rozporzadzenie w sprawie wysokosci stawki opłaty kogeneracyjnej na rok 2026"}]}""".encode()
        result = official.cogeneration_document_from_eli(content, 2026)
        self.assertEqual(
            "https://api.sejm.gov.pl/eli/acts/DU/2025/1664/text.pdf", result.url
        )

    def test_parses_row_oriented_tauron_tariff(self) -> None:
        text = (
            """
        Taryfa TAURON Dystrybucja na rok 2026
        G11               0,2464                 7,38 10,86
        G12               0,2841       0,0558    7,38 10,86
        G12w              0,3298       0,0512    7,38 10,86
        G13               0,2203 0,3898 0,0392  7,38 10,86
        """
            + "x" * 600
        )
        with patch.object(official, "extract_pdf_text", return_value=text):
            rates = official.parse_distribution_pdf(
                b"ignored",
                "tauron",
                "G13",
                ("szczyt_przedpoludniowy", "szczyt_popoludniowy", "pozostale"),
                2026,
            )
        self.assertEqual(
            {
                "szczyt_przedpoludniowy": 0.2203,
                "szczyt_popoludniowy": 0.3898,
                "pozostale": 0.0392,
            },
            rates,
        )

    def test_rejects_partial_network_rates(self) -> None:
        text = "Taryfa 2026\nG12 0,2841\n" + "x" * 600
        with patch.object(official, "extract_pdf_text", return_value=text):
            with self.assertRaisesRegex(ValueError, "kompletnego"):
                official.parse_distribution_pdf(
                    b"ignored", "tauron", "G12", ("dzienna", "nocna"), 2026
                )


if __name__ == "__main__":
    unittest.main()
