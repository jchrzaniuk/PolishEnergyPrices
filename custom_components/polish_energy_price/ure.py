"""Read gross household energy prices from the official URE workbook.

The workbook is an offer-comparison dataset, not a distribution tariff.  It
states that columns G-L contain gross prices including both excise duty and
VAT.  This module deliberately has no Home Assistant dependencies so the
parser can be regression-tested on its own.
"""

from __future__ import annotations

from html import unescape
from io import BytesIO
import posixpath
import re
import unicodedata
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

URE_OFFERS_PAGE = (
    "https://maszwybor.ure.gov.pl/or/cenki/122,"
    "Zestawienie-ofert-sprzedawcow-energii-elektrycznej.html"
)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_N = f"{{{_MAIN_NS}}}"
MAX_UNCOMPRESSED_WORKBOOK_BYTES = 25_000_000

_SELLER_MARKERS = {
    "tauron": "tauron sprzedaz",
    "pge": "pge obrot",
    "energa": "energa obrot",
    "stoen": "e on polska",
    "enea": "enea s a",
}

_AREA_MARKERS = {
    "tauron": "tauron dystrybucja",
    "pge": "pge dystrybucja",
    "energa": "energa operator",
    "stoen": "stoen operator",
    "enea": "enea operator",
}


def _normalise(value: object) -> str:
    text = str(value or "").translate(str.maketrans({"ł": "l", "Ł": "L"}))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold()).split())


def discover_workbook_url(page_html: str, base_url: str = URE_OFFERS_PAGE) -> str:
    """Return the newest official household-offers XLSX link from a URE page."""

    candidates: list[tuple[str, str]] = []
    for raw_href in re.findall(
        r"href\s*=\s*[\"']([^\"']+\.xlsx(?:\?[^\"']*)?)[\"']",
        page_html,
        flags=re.IGNORECASE,
    ):
        url = urljoin(base_url, unescape(raw_href))
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not (
            hostname == "ure.gov.pl" or hostname.endswith(".ure.gov.pl")
        ):
            continue
        filename = posixpath.basename(parsed.path).casefold()
        if "gospodar" not in _normalise(filename):
            continue
        date_match = re.search(r"(20\d{6})", filename)
        candidates.append((date_match.group(1) if date_match else "", url))

    if not candidates:
        raise ValueError(
            "Na stronie URE nie znaleziono arkusza XLSX dla gospodarstw domowych"
        )
    return max(candidates)[1]


def _column_index(reference: str) -> int:
    result = 0
    for char in reference:
        if not char.isalpha():
            break
        result = result * 26 + ord(char.upper()) - ord("A") + 1
    return result - 1


def _shared_strings(book: ZipFile) -> list[str]:
    try:
        root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iter(f"{_N}t"))
        for item in root.findall(f"{_N}si")
    ]


def _sheet_path(book: ZipFile) -> str:
    workbook = ET.fromstring(book.read("xl/workbook.xml"))
    relationships = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.attrib["Id"]: item.attrib["Target"]
        for item in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    for sheet in workbook.findall(f"{_N}sheets/{_N}sheet"):
        name = _normalise(sheet.attrib.get("name"))
        if name not in {"stale", "ceny stale"}:
            continue
        relation_id = sheet.attrib.get(f"{{{_REL_NS}}}id")
        if relation_id not in targets:
            break
        target = targets[relation_id].lstrip("/")
        if not target.startswith("xl/"):
            target = posixpath.join("xl", target)
        path = posixpath.normpath(target)
        if not path.startswith("xl/worksheets/"):
            raise ValueError("Nieprawidłowa ścieżka arkusza URE")
        return path
    raise ValueError("Arkusz URE nie zawiera zakładki 'Stałe'")


def _cell_value(cell: ET.Element, strings: list[str]) -> object:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_N}t"))
    value = cell.find(f"{_N}v")
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        try:
            return strings[int(value.text)]
        except (IndexError, ValueError) as err:
            raise ValueError("Nieprawidłowy indeks tekstu w arkuszu URE") from err
    if cell_type in {"str", "e"}:
        return value.text
    try:
        return float(value.text)
    except ValueError:
        return value.text


def _rows(book: ZipFile) -> list[dict[int, object]]:
    strings = _shared_strings(book)
    root = ET.fromstring(book.read(_sheet_path(book)))
    result: list[dict[int, object]] = []
    for row in root.iter(f"{_N}row"):
        values = {
            _column_index(cell.attrib.get("r", "")): _cell_value(cell, strings)
            for cell in row.findall(f"{_N}c")
        }
        result.append(values)
    return result


def _price(value: object) -> float:
    if isinstance(value, (float, int)):
        result = float(value)
    else:
        try:
            result = float(str(value).strip().replace(",", "."))
        except ValueError as err:
            raise ValueError(f"Cena URE nie jest liczbą: {value!r}") from err
    if not 0 < result < 10:
        raise ValueError(f"Cena URE jest poza bezpiecznym zakresem: {result}")
    return round(result, 4)


def _contains_group(text: str, group: str) -> bool:
    return (
        re.search(rf"(?:^| )({re.escape(group.casefold())})(?: |$)", text) is not None
    )


def _matching_rows(
    rows: list[dict[int, object]], operator: str, group: str
) -> list[dict[int, object]]:
    seller = _SELLER_MARKERS[operator]
    area = _AREA_MARKERS[operator]
    matches: list[dict[int, object]] = []
    for row in rows:
        if seller not in _normalise(row.get(0)) or area not in _normalise(row.get(5)):
            continue
        if operator != "stoen" and _normalise(row.get(3)) != "regulowana":
            continue
        searchable = _normalise(f"{row.get(1, '')} {row.get(23, '')}")
        category = _normalise(row.get(2))
        if group == "G11" and category.startswith("g11"):
            # ENEA also publishes G11p and E.ON has other G11 offers.  TAURON's
            # regulated row is the only one whose title omits the group symbol.
            if operator not in {"tauron", "energa"} and not _contains_group(
                _normalise(row.get(1)), "g11"
            ):
                continue
            matches.append(row)
        elif group == "G13" and category.startswith("g13"):
            matches.append(row)
        elif category.startswith("g12") and _contains_group(searchable, group):
            matches.append(row)
    return matches


def parse_ure_workbook(content: bytes, operator: str, group: str) -> dict[str, float]:
    """Extract one default-seller tariff's gross, excise-inclusive prices."""

    if operator not in _SELLER_MARKERS:
        raise ValueError(f"Nieobsługiwany operator: {operator}")
    try:
        with ZipFile(BytesIO(content)) as book:
            unpacked_size = sum(item.file_size for item in book.infolist())
            if unpacked_size > MAX_UNCOMPRESSED_WORKBOOK_BYTES:
                raise ValueError("Rozpakowany arkusz URE przekracza bezpieczny limit")
            rows = _rows(book)
    except (BadZipFile, ET.ParseError, KeyError) as err:
        raise ValueError("Plik URE nie jest obsługiwanym arkuszem XLSX") from err

    if len(rows) < 4:
        raise ValueError("Arkusz URE nie zawiera tabeli ofert")
    header = " ".join(_normalise(value) for row in rows[:4] for value in row.values())
    if "ceny energii brutto" not in header or "z akcyza i vat" not in header:
        raise ValueError("Arkusz URE nie potwierdza cen brutto z akcyzą i VAT")

    matches = _matching_rows(rows[4:], operator, group)
    if not matches:
        raise ValueError(f"Brak regulowanej oferty URE dla {operator}:{group}")

    # In the 10 July 2026 URE file the labels of PGE G12w and G12n are swapped,
    # while the prices follow PGE's approved tariff.  The weekend tariff is the
    # higher of those two rows; this keeps the authoritative group mapping and
    # remains safe if URE later fixes only the labels.
    if operator == "pge" and group in {"G12w", "G12n"}:
        combined = _matching_rows(rows[4:], operator, "G12w") + _matching_rows(
            rows[4:], operator, "G12n"
        )
        unique = {id(row): row for row in combined}.values()
        if len(combined) >= 2:
            matches = [
                (max if group == "G12w" else min)(
                    unique, key=lambda row: _price(row.get(7)) + _price(row.get(8))
                )
            ]

    price_sets: set[tuple[float, ...]] = set()
    if group == "G11":
        for row in matches:
            price_sets.add((_price(row.get(6)),))
        zones = ("calodobowa",)
    elif group == "G13":
        for row in matches:
            price_sets.add(tuple(_price(row.get(column)) for column in (9, 10, 11)))
        zones = ("szczyt_przedpoludniowy", "szczyt_popoludniowy", "pozostale")
    else:
        for row in matches:
            price_sets.add((_price(row.get(7)), _price(row.get(8))))
        zones_by_group = {
            "G12": ("dzienna", "nocna"),
            "G12w": ("szczytowa", "pozaszczytowa")
            if operator in {"tauron", "stoen", "enea"}
            else ("dzienna", "nocna"),
            "G12n": ("dzienna", "nocna"),
            "G12r": ("szczytowa", "pozaszczytowa"),
            "G12as": ("dzienna", "nocna"),
        }
        try:
            zones = zones_by_group[group]
        except KeyError as err:
            raise ValueError(f"Nieobsługiwana grupa w arkuszu URE: {group}") from err

    if len(price_sets) != 1:
        raise ValueError(f"Niejednoznaczne ceny URE dla {operator}:{group}")
    return dict(zip(zones, price_sets.pop(), strict=True))
