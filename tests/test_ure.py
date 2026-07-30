"""Tests for the dependency-free official URE workbook parser."""

from __future__ import annotations

from html import escape
import importlib.util
from io import BytesIO
from pathlib import Path
import sys
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "polish_energy_price"
    / "ure.py"
)
SPEC = importlib.util.spec_from_file_location("polish_energy_ure", MODULE_PATH)
assert SPEC and SPEC.loader
ure = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ure
SPEC.loader.exec_module(ure)


def _cell(column: str, row: int, value: str | float) -> str:
    if isinstance(value, str):
        return (
            f'<c r="{column}{row}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
        )
    return f'<c r="{column}{row}"><v>{value}</v></c>'


def workbook(
    data_rows: list[dict[str, str | float]], *, includes_taxes: bool = True
) -> bytes:
    rows = [
        '<row r="1">'
        + _cell("A", 1, "Nazwa sprzedawcy")
        + _cell("G", 1, "Ceny energii brutto (zł/kWh)")
        + "</row>",
        '<row r="2">' + _cell("G", 2, "G11") + "</row>",
        '<row r="3">'
        + _cell("G", 3, "brutto (z akcyzą i VAT)" if includes_taxes else "brutto")
        + "</row>",
        '<row r="4">' + _cell("A", 4, "sortuj/filtruj") + "</row>",
    ]
    for number, values in enumerate(data_rows, start=5):
        rows.append(
            f'<row r="{number}">'
            + "".join(_cell(column, number, value) for column, value in values.items())
            + "</row>"
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows)}</sheetData></worksheet>"
    )
    book = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Stałe" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", book)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


def row(**values: str | float) -> dict[str, str | float]:
    return values


class UreParserTests(unittest.TestCase):
    def test_discovers_newest_official_household_workbook(self) -> None:
        page = """
        <a href="https://evil.example/20270101gospodarstwa.xlsx">evil</a>
        <a href="https://attackerure.gov.pl/20280101gospodarstwa.xlsx">lookalike</a>
        <a href="/download/4/1/20260620gospodarstwadomowe.xlsx">old</a>
        <a href="/download/4/2/20260710gospodarstwadomoweBv1.xlsx">new</a>
        """
        self.assertEqual(
            "https://maszwybor.ure.gov.pl/download/4/2/20260710gospodarstwadomoweBv1.xlsx",
            ure.discover_workbook_url(page),
        )

    def test_parses_gross_excise_inclusive_enea_prices(self) -> None:
        content = workbook(
            [
                row(
                    A="Enea S.A.",
                    B="Taryfa. Symbol grupy taryfowej G12w",
                    C="G12x (2 strefy)",
                    D="Regulowana",
                    F="ENEA Operator Sp. z o.o.",
                    H=0.8079,
                    I=0.4323,
                )
            ]
        )
        self.assertEqual(
            {"szczytowa": 0.8079, "pozaszczytowa": 0.4323},
            ure.parse_ure_workbook(content, "enea", "G12w"),
        )

    def test_keeps_pge_groups_correct_when_ure_labels_are_swapped(self) -> None:
        content = workbook(
            [
                row(
                    A="PGE Obrót S.A.", B="Taryfa (G12N)", C="G12x (2 strefy)",
                    D="Regulowana", F="PGE Dystrybucja S.A.", H=0.7221, I=0.5271,
                ),
                row(
                    A="PGE Obrót S.A.", B="Taryfa (G12W)", C="G12x (2 strefy)",
                    D="Regulowana", F="PGE Dystrybucja S.A.", H=0.6840, I=0.4873,
                ),
            ]
        )
        self.assertEqual(
            {"dzienna": 0.7221, "nocna": 0.5271},
            ure.parse_ure_workbook(content, "pge", "G12w"),
        )
        self.assertEqual(
            {"dzienna": 0.6840, "nocna": 0.4873},
            ure.parse_ure_workbook(content, "pge", "G12n"),
        )

    def test_rejects_workbook_without_excise_and_vat_declaration(self) -> None:
        content = workbook([], includes_taxes=False)
        with self.assertRaisesRegex(ValueError, "akcyzą i VAT"):
            ure.parse_ure_workbook(content, "enea", "G11")


if __name__ == "__main__":
    unittest.main()
