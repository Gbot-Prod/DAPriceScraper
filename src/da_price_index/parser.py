"""PDF parser for DA Daily Price Index reports.

Extracts commodity price tables from DPI PDF files using pdfplumber's
line-based table detection, maintaining a category hierarchy state machine
across page breaks and handling the various layout quirks observed in
real-world DA publications.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

from da_price_index.config import settings
from da_price_index.models import ParsedPriceRow, ParsedReport

# ---------------------------------------------------------------------------
# Known category hierarchy constants
# ---------------------------------------------------------------------------

TOP_CATEGORIES: set[str] = {
    "IMPORTED COMMERCIAL RICE",
    "LOCAL COMMERCIAL RICE",
    "CORN PRODUCTS",
    "LEGUMES",
    "FISHERIES AND SEAFOOD",
    "LIVESTOCK & POULTRY PRODUCTS",
    "LOWLAND VEGETABLES",
    "HIGHLAND VEGETABLES",
    "SPICES",
    "FRUITS",
    "ROOTCROPS",
    "OTHER BASIC COMMODITIES",
}

SUB_CATEGORIES: set[str] = {
    "BEEF",
    "BEEF OFFALS",
    "BUFFALO",
    "PORK",
    "PORK OFFALS",
    "LAMB",
    "POULTRY PRODUCTS",
}

GROUP_CATEGORIES: set[str] = {
    "CHICKEN",
    "DUCK",
    "EGG",
    "CHICKEN OFFALS",
}

# Items whose unit is NOT kg
_EGG_COMMODITIES_LOWER = {"egg", "chicken egg", "duck egg"}
_COOKING_OIL_PREFIX = "cooking oil"

# ---------------------------------------------------------------------------
# Header detection
# ---------------------------------------------------------------------------

_HEADER_PATTERNS = [
    re.compile(r"^COMMODITY", re.IGNORECASE),
    re.compile(r"PREVAILING\s+RETAIL", re.IGNORECASE),
    re.compile(r"P\s*/\s*UNIT", re.IGNORECASE),
    re.compile(r"PRICE\s+PER\s+UNIT", re.IGNORECASE),
]


def _is_header_row(row: list[str | None]) -> bool:
    """Return True if the row looks like a repeated column header."""
    combined = " ".join(str(c) for c in row if c)
    combined = re.sub(r"\s+", " ", combined).strip()
    return any(p.search(combined) for p in _HEADER_PATTERNS)


def _is_all_none_or_empty(row: list[str | None]) -> bool:
    """Check if every cell is None or empty string."""
    return all(c is None or str(c).strip() == "" for c in row)


# ---------------------------------------------------------------------------
# Cell normalisation
# ---------------------------------------------------------------------------

def _norm(cell: str | None) -> str:
    """Normalise a PDF cell value: collapse whitespace, strip."""
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

def _parse_price(raw: str) -> tuple[Decimal | None, bool]:
    """Parse a price cell, returning (price, is_available).

    - ``n/a`` (case-insensitive) → (None, False)
    - Valid numeric → (Decimal, True)
    - Empty → (None, False)
    """
    s = raw.strip()
    if not s:
        return None, False
    if re.match(r"^n\s*/?\s*a$", s, re.IGNORECASE):
        return None, False
    # Strip any stray non-numeric chars (commas, peso signs, etc.)
    cleaned = re.sub(r"[^\d.]", "", s)
    if not cleaned:
        return None, False
    try:
        return Decimal(cleaned), True
    except InvalidOperation:
        return None, False


# ---------------------------------------------------------------------------
# Unit derivation (from PDF Note c)
# ---------------------------------------------------------------------------

def _derive_unit(commodity_name: str, top_category: str, group_category: str | None) -> str:
    """Derive the measurement unit based on DA Note c) rules.

    - Eggs → 'pc'
    - Cooking Oil → 'L'
    - Everything else (agri-fishery, salt, sugar) → 'kg'
    """
    name_lower = commodity_name.lower()
    # Egg check: group category or commodity name
    if group_category and group_category.upper() == "EGG":
        return "pc"
    for egg_kw in _EGG_COMMODITIES_LOWER:
        if egg_kw in name_lower:
            return "pc"
    # Cooking oil check
    if name_lower.startswith(_COOKING_OIL_PREFIX):
        return "L"
    return "kg"


# ---------------------------------------------------------------------------
# Header metadata extraction
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(
    r"\(\s*([A-Za-z]+),\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})\s*\)"
)
# Dates embedded in portal file names, e.g. "Daily-Price-Index-November-25-2026.pdf"
# or "November-24-2026-DPI-AFC.pdf".
_FILENAME_DATE_RE = re.compile(
    r"(?<![A-Za-z])([A-Za-z]{3,9})[-_ .]+(\d{1,2})[-_ .,]+(\d{4})(?!\d)"
)
_REGION_RE = re.compile(
    r"(National Capital Region\s*\(NCR\))", re.IGNORECASE
)


def _extract_header_metadata(page_text: str) -> tuple[date, str, str, str]:
    """Extract report_date, weekday, region_name, and region_code from Page 1 text."""
    # Date
    m = _DATE_RE.search(page_text)
    if not m:
        raise ValueError("Could not find report date in Page 1 header text")
    header_weekday = m.group(1)
    report_date = datetime.strptime(m.group(2), "%B %d, %Y").date()

    # Region
    rm = _REGION_RE.search(page_text)
    region_name = rm.group(1).strip() if rm else "National Capital Region (NCR)"

    # Derive code from name
    code_m = re.search(r"\(([A-Z]+)\)", region_name)
    region_code = code_m.group(1) if code_m else "NCR"

    return report_date, header_weekday, region_name, region_code


def _extract_filename_date(file_name: str) -> date | None:
    """Return the date embedded in a DPI file name, or None if there isn't one."""
    for m in _FILENAME_DATE_RE.finditer(file_name):
        month, day, year = m.groups()
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(f"{month} {day} {year}", fmt).date()
            except ValueError:
                continue
    return None


def _reconcile_report_date(
    header_date: date, header_weekday: str, file_name: str
) -> tuple[date, list[str]]:
    """Cross-check the header date against the file-name date.

    DA occasionally publishes PDFs whose header date is a typo (e.g. the
    November 25, 2026 report was headed "Wednesday, November 13, 2026").
    The portal file name has proven more reliable, so when the two disagree
    the file-name date wins and a warning is returned for the logs.
    """
    warnings: list[str] = []
    weekday_ok = header_date.strftime("%A").lower() == header_weekday.lower()

    filename_date = _extract_filename_date(file_name)
    if filename_date is None:
        if not weekday_ok:
            warnings.append(
                f"Header date {header_date} is a {header_date:%A}, but the header says "
                f"{header_weekday}; no date in file name {file_name!r} to cross-check."
            )
        return header_date, warnings

    if filename_date != header_date:
        warnings.append(
            f"Header date {header_date} ({header_weekday}) disagrees with file name "
            f"{file_name!r}; using file-name date {filename_date} ({filename_date:%A})."
        )
        return filename_date, warnings

    return header_date, warnings


# ---------------------------------------------------------------------------
# Covered markets extraction (last page footer)
# ---------------------------------------------------------------------------

_MARKET_LINE_RE = re.compile(
    r"(\d+)\.\s+(.+?)(?:\s{2,}(\d+)\.\s+(.+))?$"
)


def _extract_covered_markets(page_text: str) -> list[str]:
    """Parse the numbered covered-market list from the last page's notes."""
    lines = page_text.split("\n")
    in_markets = False
    market_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "d) Covered market" in stripped:
            in_markets = True
            continue
        if in_markets:
            if stripped.lower().startswith("page "):
                break
            market_lines.append(stripped)

    full_block = "\n".join(market_lines)
    # Match all occurrences: number dot space name
    matches = re.findall(r"(\d+)\.\s+(.*?)(?=(?:\s+\d+\.|$))", full_block, re.DOTALL)
    markets: dict[int, str] = {
        int(num): re.sub(r"\s+", " ", name).strip() for num, name in matches
    }
    return [markets[k] for k in sorted(markets)]


# ---------------------------------------------------------------------------
# Row normalisation — handle varying column layouts
# ---------------------------------------------------------------------------

def _normalise_table_row(row: list[str | None]) -> tuple[str, str, str]:
    """Extract (commodity, specification, price) from a table row.

    Handles both 3-column layout (Nov 22) and 5-column layout (Nov 23/24)
    where the extra columns are filled with None.
    """
    # Filter out None-only interior columns:
    # 3-col: [commodity, spec, price]
    # 5-col: [commodity, None, None, spec, price]  (or category rows with values in col 1)
    cells = [_norm(c) for c in row]

    if len(cells) == 3:
        return cells[0], cells[1], cells[2]
    elif len(cells) == 5:
        # In 5-col layout:
        # Normal data rows:   [commodity, '', '', spec, price]
        # Category rows:      ['', 'LIVESTOCK & POULTRY', '', '', '']
        #                     [None, 'PRODUCTS', None, None, None]
        col0 = cells[0]
        col1 = cells[1]
        col3 = cells[3]
        col4 = cells[4]

        # If col0 is empty but col1 has text, this is a category header spread across cols
        if not col0 and col1:
            return col1, col3, col4
        return col0, col3, col4
    else:
        # Fallback: take first, second-to-last, and last
        if len(cells) >= 3:
            return cells[0], cells[-2], cells[-1]
        return cells[0] if cells else "", "", ""


# ---------------------------------------------------------------------------
# Category state machine
# ---------------------------------------------------------------------------

def _classify_category(text: str) -> str | None:
    """Classify text into a category tier, or None if it's a commodity row."""
    upper = text.upper().strip()
    if upper in TOP_CATEGORIES:
        return "top"
    if upper in SUB_CATEGORIES:
        return "sub"
    if upper in GROUP_CATEGORIES:
        return "group"
    return None


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_dpi_pdf(pdf_path: str | Path) -> ParsedReport:
    """Parse a DA Daily Price Index PDF into a structured ParsedReport.

    Parameters
    ----------
    pdf_path : str or Path
        Path to the DPI PDF file.

    Returns
    -------
    ParsedReport
        Fully parsed and validated report with all commodity price rows.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Compute SHA-256
    file_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    with pdfplumber.open(str(pdf_path)) as pdf:
        total_pages = len(pdf.pages)

        # --- Page 1 header metadata ---
        page1_text = pdf.pages[0].extract_text() or ""
        header_date, header_weekday, region_name, region_code = _extract_header_metadata(page1_text)
        report_date, date_warnings = _reconcile_report_date(
            header_date, header_weekday, pdf_path.name
        )

        # --- Last page footer: covered markets ---
        last_page_text = pdf.pages[-1].extract_text() or ""
        covered_markets = _extract_covered_markets(last_page_text)

        # --- Category state tracker ---
        current_top: str = ""
        current_sub: str | None = None
        current_group: str | None = None

        # Accumulator to track multi-line category headers
        _pending_category_fragment: str = ""

        items: list[ParsedPriceRow] = []
        global_row_order = 0

        for page_idx in range(total_pages):
            page = pdf.pages[page_idx]
            page_num = page_idx + 1

            tables = page.extract_tables({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
            })

            if not tables:
                continue

            # Only process the first (main data) table; T1 is typically
            # the floating header remnant
            main_table = tables[0]

            for row in main_table:
                if _is_all_none_or_empty(row):
                    continue
                if _is_header_row(row):
                    continue

                commodity_raw, spec_raw, price_raw = _normalise_table_row(row)

                # --- Handle multi-row category headers ---
                # Some categories wrap across 2 rows (e.g., "IMPORTED COMMERCIAL" / "RICE",
                # "LIVESTOCK & POULTRY" / "PRODUCTS").
                # Detection: all cells are None except col1 (5-col), or the row is entirely
                # the continuation of a category name.

                # Check if this row is a continuation fragment
                # (all cells from the original row are None except one with uppercase text)
                raw_cells = [_norm(c) for c in row]
                non_empty = [c for c in raw_cells if c]

                if len(non_empty) == 1 and non_empty[0].isupper() and not price_raw:
                    # Could be a continuation of a pending category fragment
                    fragment = non_empty[0]
                    if _pending_category_fragment:
                        combined = _pending_category_fragment + " " + fragment
                        tier = _classify_category(combined)
                        if tier:
                            if tier == "top":
                                current_top = combined
                                current_sub = None
                                current_group = None
                            elif tier == "sub":
                                current_sub = combined
                                current_group = None
                            elif tier == "group":
                                current_group = combined
                            _pending_category_fragment = ""
                            continue
                    # Start a new pending fragment
                    _pending_category_fragment = fragment
                    # But also check if it's a complete category on its own
                    tier = _classify_category(fragment)
                    if tier:
                        if tier == "top":
                            current_top = fragment
                            current_sub = None
                            current_group = None
                        elif tier == "sub":
                            current_sub = fragment
                            current_group = None
                        elif tier == "group":
                            current_group = fragment
                        _pending_category_fragment = ""
                    continue

                # Clear pending fragment if we got a data row
                _pending_category_fragment = ""

                # --- Check if this is a category header row ---
                # Category rows: commodity cell is uppercase, spec and price are empty
                if commodity_raw and not spec_raw and not price_raw:
                    upper = commodity_raw.upper().strip()
                    tier = _classify_category(upper)
                    if tier:
                        if tier == "top":
                            current_top = upper
                            current_sub = None
                            current_group = None
                        elif tier == "sub":
                            current_sub = upper
                            current_group = None
                        elif tier == "group":
                            current_group = upper
                        continue
                    # Heuristic fallback: if all-caps and no price, treat as category
                    if commodity_raw.isupper() and len(commodity_raw) > 2:
                        # Unknown category — log but don't skip
                        tier_guess = _classify_category(commodity_raw)
                        if tier_guess:
                            continue
                        # Could be a new unknown top-level
                        current_top = commodity_raw
                        current_sub = None
                        current_group = None
                        continue

                # --- It's a data row ---
                if not commodity_raw:
                    continue  # skip empty rows

                if not current_top:
                    continue  # skip rows before first category

                price, is_available = _parse_price(price_raw)
                unit = _derive_unit(commodity_raw, current_top, current_group)

                global_row_order += 1
                row_obj = ParsedPriceRow(
                    top_category=current_top,
                    sub_category=current_sub,
                    group_category=current_group,
                    commodity_name=commodity_raw,
                    specification=spec_raw,
                    unit=unit,
                    price_prevailing=price,
                    is_available=is_available,
                    raw_price_text=price_raw,
                    page_number=page_num,
                    row_order=global_row_order,
                )
                items.append(row_obj)

    return ParsedReport(
        region_code=region_code,
        region_name=region_name,
        report_date=report_date,
        header_date=header_date,
        date_warnings=date_warnings,
        file_name=pdf_path.name,
        file_sha256=file_sha256,
        covered_markets=covered_markets,
        items=items,
        parser_version=settings.parser_version,
    )
