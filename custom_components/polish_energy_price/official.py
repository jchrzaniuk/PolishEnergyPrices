"""Discovery and parsing of official Polish distribution tariffs.

The parsers are deliberately strict.  An unknown document layout must leave the
last-known-good rates in place instead of silently turning an unrelated number
from a PDF into an electricity price.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
import json
import re
from urllib.parse import urlencode, urljoin, urlparse

from pypdf import PdfReader
from pypdf.errors import PdfReadError


OZE_RATES_PAGE = "https://bip.ure.gov.pl/bip/odnawialne-zrodla-energ/stawki-oplaty-oze"
ELI_API = "https://api.sejm.gov.pl/eli"
TAURON_G13S_PAGE = "https://www.tauron.pl/dla-domu/prad/prad-z-usluga/tanie-godziny"
TAURON_G14DYNAMIC_PRICE_LIST = (
    "https://www.tauron.pl/-/media/offer-documents/produkty/g14dynamic/ts/"
    "cennik/ee-gd-bsc-b-dynd-ts-0.ashx"
)

OPERATOR_PAGES: dict[str, str] = {
    "tauron": (
        "https://www.tauron-dystrybucja.pl/uslugi-dystrybucyjne/dokumenty-do-pobrania"
    ),
    "energa": "https://www.energa.pl/dom/obsluga/taryfy-i-cenniki",
    "stoen": "https://www.stoen.pl/strona/dokumenty",
    # This official ENERGA customer page mirrors documents of the other OSDs.
    # It is used for PGE because the PGE site has repeatedly served an invalid
    # TLS certificate chain, and for ENEA because it exposes the concise G extract.
    "pge": "https://www.energa.pl/dom/obsluga/taryfy-i-cenniki",
    "enea": "https://www.energa.pl/dom/obsluga/taryfy-i-cenniki",
}


@dataclass(frozen=True, slots=True)
class DocumentLink:
    """One downloadable document discovered on an official landing page."""

    url: str
    label: str


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        href = values.get("href")
        if tag == "a" and href:
            self._href = href
            self._text = []
        elif tag == "eon-ui-link" and href:
            self.links.append((href, values.get("text") or ""))

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._text)))
            self._href = None
            self._text = []


def _links(page: str, base_url: str) -> list[DocumentLink]:
    parser = _LinkParser()
    parser.feed(page)
    result: list[DocumentLink] = []
    seen: set[str] = set()
    for href, label in parser.links:
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        result.append(DocumentLink(url, " ".join(label.split())))
    return result


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc)


def discover_tauron_g13s_script(
    page: str, base_url: str = TAURON_G13S_PAGE
) -> str:
    """Find the official JavaScript table containing current G13s prices."""

    candidates = re.findall(
        r"<script[^>]+src=[\"']([^\"']*taryfa-g13s[^\"']*\.js(?:\?[^\"']*)?)[\"']",
        page,
        re.IGNORECASE,
    )
    for candidate in candidates:
        url = urljoin(base_url, candidate.replace("&amp;", "&"))
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname in {"tauron.pl", "www.tauron.pl"}:
            return url
    raise ValueError("Na stronie TAURON nie znaleziono tabeli cen G13s")


def parse_tauron_g13s_prices(script: str) -> dict[str, float]:
    """Parse gross hourly energy prices from the official G13s price table."""

    block = re.search(
        r"sellingPrices\s*:\s*\{(.*?)\}\s*,\s*distributionPrices",
        script,
        re.DOTALL,
    )
    if not block:
        raise ValueError("Tabela TAURON nie zawiera cen sprzedażowych G13s")

    periods = (
        "zima_dzien_roboczy",
        "zima_dzien_wolny",
        "lato_dzien_roboczy",
        "lato_dzien_wolny",
    )
    result: dict[str, float] = {}
    for index, period in enumerate(periods, start=1):
        row = re.search(rf"(?m)^\s*{index}\s*:\s*\[(.*?)\]", block.group(1), re.DOTALL)
        if not row:
            raise ValueError(f"Brak zestawu cen G13s nr {index}")
        values = [
            float(value.replace(",", "."))
            for value in re.findall(r"[\"']([0-9]+[,.][0-9]{4})[\"']", row.group(1))
        ]
        if (
            len(values) != 4
            or values[0] != values[2]
            or any(not 0 < value < 10 for value in values)
        ):
            raise ValueError(f"Niewiarygodny zestaw cen G13s nr {index}")
        result[f"{period}_dzienna_pozaszczytowa"] = values[1]
        result[f"{period}_dzienna_szczytowa"] = values[0]
        result[f"{period}_nocna"] = values[3]
    return result


def parse_tauron_g14dynamic_prices(content: bytes) -> dict[str, float]:
    """Read the single-zone gross energy price from the official offer PDF."""

    text = extract_pdf_text(content)
    gross = re.search(r"\(brutto\)", text, re.IGNORECASE)
    if not gross:
        raise ValueError("Cennik G14dynamic nie zawiera tabeli cen brutto")
    block = text[gross.end() : gross.end() + 6000]
    values = [
        float(value.replace(",", "."))
        for value in re.findall(r"(?<![\d.])\d+[,.]\d{4}(?!\d)", block)
    ]
    if len(values) < 4 or len(set(values[:4])) != 1:
        raise ValueError("Cennik G14dynamic nie zawiera jednej ceny dla czterech stref")
    price = round(values[0], 4)
    if not 0 < price < 10:
        raise ValueError("Cena energii G14dynamic jest poza bezpiecznym zakresem")
    return {
        "S1_zalecane_uzytkowanie": price,
        "S2_normalne": price,
        "S3_zalecane_oszczedzanie": price,
        "S4_wymagane_ograniczenie": price,
    }


def discover_distribution_document(
    page: str, base_url: str, operator: str, year: int
) -> DocumentLink:
    """Choose the most useful current tariff PDF from an official page."""

    operator_terms = {
        "tauron": ("tauron dystrybuc",),
        "pge": ("pge dystrybuc",),
        "energa": ("energa-operator", "energa operator"),
        "stoen": ("stoen", "taryfa dla dystrybucji"),
        "enea": ("enea operator",),
    }[operator]
    ranked: list[tuple[int, DocumentLink]] = []
    for link in _links(page, base_url):
        haystack = f"{link.label} {link.url}".lower()
        if not _is_http_url(link.url) or not any(x in haystack for x in operator_terms):
            continue
        if str(year) not in haystack or "taryf" not in haystack:
            continue
        if not (".pdf" in haystack or ".ashx" in haystack):
            continue
        score = 0
        score += 12 if "wyciąg" in haystack or "wyciag" in haystack else 0
        score += 10 if "grup" in haystack and " g" in haystack else 0
        score += 8 if "ocr" in haystack else 0
        score += 4 if "zaktualiz" in haystack else 0
        score -= 30 if "brutto" in haystack else 0
        score -= 40 if "decyz" in haystack or "podpis" in haystack else 0
        score -= 50 if "archiw" in haystack else 0
        score -= (
            80
            if any(
                str(other) in haystack
                for other in range(year - 2, year + 3)
                if other != year
            )
            else 0
        )
        # The concise extracts have the most stable row layout for TAURON/ENEA.
        ranked.append((score, link))
    if not ranked:
        raise ValueError(f"Nie znaleziono taryfy {operator.upper()} na {year} r.")
    return max(ranked, key=lambda item: item[0])[1]


def discover_quality_document(
    page: str, base_url: str, operator: str, year: int, tariff: DocumentLink
) -> DocumentLink:
    """Prefer an OSD document containing the latest quality-rate change."""

    operator_terms = {
        "tauron": ("tauron", "jakościowej"),
        "pge": ("pge",),
        "energa": ("energa-operator", "energa operator"),
        "stoen": ("stoen", "taryfa dla dystrybucji"),
        "enea": ("enea operator",),
    }[operator]
    candidates: list[tuple[int, DocumentLink]] = []
    for link in _links(page, base_url):
        value = f"{link.label} {link.url}".lower()
        if str(year) not in value or not any(term in value for term in operator_terms):
            continue
        if not (".pdf" in value or ".ashx" in value):
            continue
        score = 20 if "jakościow" in value else 0
        score += 15 if "zaktualiz" in value or "tekst jednolity" in value else 0
        score += 5 if "wyciąg" in value or "wyciag" in value else 0
        score -= 20 if "brutto" in value else 0
        score -= (
            80
            if any(
                str(other) in value
                for other in range(year - 2, year + 3)
                if other != year
            )
            else 0
        )
        candidates.append((score, link))
    best = max(candidates, default=(0, tariff), key=lambda item: item[0])
    return best[1] if best[0] > 0 else tariff


def oze_rate_from_page(page: str, year: int) -> float:
    """Read the annual net OZE rate from the structured BIP URE table."""

    match = re.search(
        rf"<td[^>]*>\s*{year}\s*</td>\s*<td[^>]*>\s*([0-9]+[,.][0-9]+)\s*</td>",
        page,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError(f"Brak stawki OZE BIP URE na {year} r.")
    rate = float(match.group(1).replace(",", ".")) / 1000
    if not 0 <= rate <= 0.1:
        raise ValueError("Stawka OZE poza bezpiecznym zakresem")
    return round(rate, 6)


def eli_search_url(year: int) -> str:
    """Build the official ELI query for an annual cogeneration regulation."""

    query = urlencode(
        {
            "publisher": "DU",
            # The regulation is normally published late in the preceding year.
            "year": year - 1,
            "title": f"wysokości stawki opłaty kogeneracyjnej na rok {year}",
        }
    )
    return f"{ELI_API}/acts/search?{query}"


def cogeneration_document_from_eli(content: bytes, year: int) -> DocumentLink:
    """Resolve the official regulation PDF from an ELI API result."""

    try:
        result = json.loads(content)
        items = result["items"]
    except (KeyError, TypeError, json.JSONDecodeError) as err:
        raise ValueError("Nieprawidłowa odpowiedź API ELI") from err
    expected = f"opłaty kogeneracyjnej na rok {year}"
    matching = [
        item for item in items if expected in str(item.get("title", "")).lower()
    ]
    if len(matching) != 1 or not matching[0].get("ELI"):
        raise ValueError(f"Nie znaleziono rozporządzenia kogeneracyjnego na {year} r.")
    eli = str(matching[0]["ELI"])
    return DocumentLink(
        f"{ELI_API}/acts/{eli}/text.pdf",
        str(matching[0].get("title", eli)),
    )


def extract_pdf_text(content: bytes) -> str:
    """Extract layout-preserving text from a bounded PDF downloaded by HA."""

    if not content.startswith(b"%PDF"):
        raise ValueError("Pobrany dokument nie jest plikiem PDF")
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 250:
            raise ValueError("Nieobsługiwany dokument PDF")
        text = "\n".join(
            page.extract_text(extraction_mode="layout") or "" for page in reader.pages
        )
    except PdfReadError as err:
        raise ValueError("Uszkodzony albo nieobsługiwany dokument PDF") from err
    if len(text) < 500:
        raise ValueError("Dokument PDF nie ma użytecznej warstwy tekstowej")
    return text


def _decimal_values(text: str) -> list[float]:
    # Some embedded fonts split the last two digits: ``0,23 42``.
    normalized = re.sub(r"(\d+[,.]\d{1,3})[ \u00a0]+(\d{1,3})(?![\d,])", r"\1\2", text)
    return [
        float(value.replace(",", "."))
        for value in re.findall(r"\d+[,.]\d+", normalized)
    ]


def _validate_distribution(values: list[float], zones: int) -> list[float]:
    if len(values) != zones or any(not 0.001 <= value <= 5 for value in values):
        raise ValueError("Niepełny lub niewiarygodny zestaw stawek sieciowych")
    return [round(value, 6) for value in values]


def _four_decimal_values(text: str) -> list[float]:
    return [
        float(value.replace(",", "."))
        for value in re.findall(r"\d+[,.]\d{4}(?!\d)", text)
    ]


def _row_rates(text: str, group: str, zones: int) -> list[float]:
    candidates: list[list[float]] = []
    for match in re.finditer(rf"(?mi)^\s*{re.escape(group)}\s+([^\n]+)$", text):
        values = [
            value for value in _four_decimal_values(match.group(1)) if value < 1.5
        ]
        if len(values) >= zones:
            candidates.append(values[:zones])
    if candidates:
        return _validate_distribution(max(candidates, key=sum), zones)
    raise ValueError(f"Nie znaleziono kompletnego wiersza {group}")


def _tauron_g13s_rates(text: str, zone_keys: tuple[str, ...]) -> dict[str, float]:
    """Read the four seasonal/day-type G13s network-rate rows."""

    names = (
        ("lato", "dzień roboczy", "lato_dzien_roboczy"),
        ("lato", "dzień wolny", "lato_dzien_wolny"),
        ("zima", "dzień roboczy", "zima_dzien_roboczy"),
        ("zima", "dzień wolny", "zima_dzien_wolny"),
    )
    result: dict[str, float] = {}
    for season, day_type, key in names:
        row = re.search(
            rf"(?mi)^\s*G13s\s*\(\s*{season}\s*,\s*{day_type}[^)]*\)\s+([^\n]+)$",
            text,
        )
        if not row:
            raise ValueError(f"Nie znaleziono wiersza G13s ({season}, {day_type})")
        values = [value for value in _four_decimal_values(row.group(1)) if value < 1.5]
        values = _validate_distribution(values[:3], 3)
        result[f"{key}_dzienna_pozaszczytowa"] = values[0]
        result[f"{key}_dzienna_szczytowa"] = values[1]
        result[f"{key}_nocna"] = values[2]
    if set(result) != set(zone_keys):
        raise ValueError("Niepełny zestaw stref dystrybucyjnych G13s")
    return {zone: result[zone] for zone in zone_keys}


def _tauron_g14dynamic_rates(
    text: str, zone_keys: tuple[str, ...]
) -> dict[str, float]:
    """Read the four Kompas-dependent network rates from the TAURON row."""

    for match in re.finditer(r"G14dynamic", text, re.IGNORECASE):
        values = _four_decimal_values(text[match.end() : match.end() + 2500])[:4]
        if (
            len(values) == 4
            and values[0] < values[1] < values[2] < values[3]
            and values[3] > 1
        ):
            validated = _validate_distribution(values, 4)
            return dict(zip(zone_keys, validated, strict=True))
    raise ValueError("Nie znaleziono kompletnego wiersza G14dynamic")


def _enea_rates(text: str, group: str, zones: int) -> list[float]:
    block = re.search(rf"(?ms)^\s*{re.escape(group)}\b(.*?)(?=^\s*G[0-9][^\n]*$)", text)
    if not block:
        raise ValueError(f"Nie znaleziono sekcji {group}")
    values = [value for value in _four_decimal_values(block.group(1)) if value < 1.5]
    return _validate_distribution(values[:zones], zones)


def _column_positions(
    header: str, groups: tuple[str, ...]
) -> dict[str, tuple[int, int]]:
    starts = [(group, header.find(group)) for group in groups]
    if any(position < 0 for _, position in starts):
        raise ValueError("Niepełny nagłówek grup taryfowych")
    result: dict[str, tuple[int, int]] = {}
    for index, (group, position) in enumerate(starts):
        left = 0 if index == 0 else (starts[index - 1][1] + position) // 2
        right = (
            len(header)
            if index + 1 == len(starts)
            else (position + starts[index + 1][1]) // 2
        )
        result[group] = (left, right)
    return result


def _line_containing(text: str, term: str) -> str:
    for line in text.splitlines():
        if term.lower() in line.lower():
            return line
    raise ValueError(f"Nie znaleziono wiersza {term}")


def _pge_rates(text: str, group: str, zones: int) -> list[float]:
    headers = [line for line in text.splitlines() if "G11" in line and "G12n" in line]
    if not headers:
        raise ValueError("Nie znaleziono tabeli stawek PGE")
    groups = ("G11", "G12", "G12as", "G12n", "G12w", "G12e")
    bounds = _column_positions(headers[-1], groups)[group]
    terms = ("całodobowy",) if zones == 1 else ("dzienny", "nocny")
    values = []
    for term in terms:
        row = _line_containing(text[text.find(headers[-1]) :], term)
        parsed = _decimal_values(row[bounds[0] : bounds[1]])
        if len(parsed) != 1:
            raise ValueError(f"Niejednoznaczna stawka PGE {group}/{term}")
        values.append(parsed[0])
    return _validate_distribution(values, zones)


def _stoen_rates(text: str, group: str, zones: int) -> list[float]:
    headers = [line for line in text.splitlines() if "G11" in line and "G12eko" in line]
    if not headers:
        raise ValueError("Nie znaleziono tabeli stawek Stoen")
    groups = ("G11", "G12", "G12w", "G12as", "G12eko")
    bounds = _column_positions(headers[-1], groups)[group]
    if group == "G11":
        terms = ("jednostrefowy",)
    elif group == "G12w":
        terms = ("szcz y towa", "pozaszcz y towa")
    else:
        terms = ("dzienna", "nocna")
    values = []
    table = text[text.find(headers[-1]) :]
    for term in terms:
        row = _line_containing(table, term)
        parsed = _decimal_values(row[bounds[0] : bounds[1]])
        if not parsed:
            raise ValueError(f"Brak stawki Stoen {group}/{term}")
        values.append(parsed[0])
    # G12as has a discounted rate only for consumption above a historic
    # baseline, which cannot be inferred from a meter state. Keep the baseline
    # rate in both zones, matching the documented integration behaviour.
    if group == "G12as":
        values[1] = values[0]
    return _validate_distribution(values, zones)


def parse_distribution_pdf(
    content: bytes, operator: str, group: str, zone_keys: tuple[str, ...], year: int
) -> dict[str, float]:
    """Parse the selected G-group network rates from an official OSD PDF."""

    text = extract_pdf_text(content)
    if str(year) not in text:
        raise ValueError(f"Dokument nie potwierdza roku {year}")
    zones = len(zone_keys)
    if operator == "tauron" and group.lower() == "g13s":
        return _tauron_g13s_rates(text, zone_keys)
    if operator == "tauron" and group.lower() == "g14dynamic":
        return _tauron_g14dynamic_rates(text, zone_keys)
    if operator in ("tauron", "energa"):
        values = _row_rates(text, group, zones)
    elif operator == "enea":
        values = _enea_rates(text, group, zones)
    elif operator == "pge":
        values = _pge_rates(text, group, zones)
    elif operator == "stoen":
        values = _stoen_rates(text, group, zones)
    else:
        raise ValueError(f"Nieobsługiwany OSD: {operator}")
    return dict(zip(zone_keys, values, strict=True))


def parse_quality_rate_pdf(content: bytes, year: int) -> float:
    """Read the current net quality rate published by the selected OSD."""

    text = extract_pdf_text(content)
    if str(year) not in text:
        raise ValueError(f"Dokument OSD nie potwierdza roku {year}")
    values: list[float] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "jakościow" not in line.lower():
            continue
        window = " ".join(lines[index : index + 15])
        values.extend(
            value for value in _decimal_values(window) if 0.005 <= value <= 0.2
        )
    if not values:
        raise ValueError("Dokument OSD nie zawiera bieżącej stawki jakościowej")
    return round(values[-1], 6)


def parse_cogeneration_rate_pdf(content: bytes, year: int) -> float:
    """Read the annual net rate from the official regulation PDF."""

    text = extract_pdf_text(content)
    match = re.search(
        rf"stawki\s+opłaty\s+kogeneracyjnej\s+na\s+rok\s+{year}",
        text,
        re.IGNORECASE,
    )
    value = re.search(
        rf"na\s+rok\s+{year}\s+wynosi\s+([0-9]+[,.][0-9]+)\s*zł/MWh",
        text,
        re.IGNORECASE,
    )
    if not match or not value:
        raise ValueError("Rozporządzenie nie zawiera stawki kogeneracyjnej")
    rate = float(value.group(1).replace(",", ".")) / 1000
    if not 0 <= rate <= 0.1:
        raise ValueError("Stawka kogeneracyjna poza bezpiecznym zakresem")
    return round(rate, 6)
