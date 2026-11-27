"""Unit tests for DA DPI PDF parser."""

from datetime import date
from decimal import Decimal
from pathlib import Path

try:
    import pytest
except ImportError:
    pytest = None

from da_price_index.parser import _extract_filename_date, _reconcile_report_date, parse_dpi_pdf

FIXTURES_DIR = Path(__file__).parent / "fixtures"

PDF_NOV_22 = FIXTURES_DIR / "Daily-Price-Index-November-22-2026.pdf"
PDF_NOV_23 = FIXTURES_DIR / "Daily-Price-Index-November-23-2026.pdf"
PDF_NOV_24 = FIXTURES_DIR / "November-24-2026-DPI-AFC.pdf"
# Real DA upload whose header misprints the date as "Wednesday, November 13, 2026".
PDF_NOV_25_MISDATED = FIXTURES_DIR / "Daily-Price-Index-November-25-2026.pdf"


def _get_item(report, name: str, category: str | None = None):
    for item in report.items:
        if name.lower() in item.commodity_name.lower():
            if category is None or category.lower() in item.top_category.lower():
                return item
    return None


class TestDPIParser:
    """Test PDF parsing on all 3 reference files."""

    def test_parse_nov_22(self):
        report = parse_dpi_pdf(PDF_NOV_22)
        assert report.report_date == date(2026, 11, 22)
        assert report.region_code == "NCR"
        assert len(report.covered_markets) == 30
        assert len(report.items) >= 100

        # Spot-checked items from acceptance criteria
        basmati = _get_item(report, "Basmati")
        assert basmati is not None
        assert basmati.price_prevailing == Decimal("150.00")
        assert basmati.is_available is True
        assert basmati.unit == "kg"
        assert basmati.top_category == "IMPORTED COMMERCIAL RICE"

        well_milled = _get_item(report, "Well Milled", "LOCAL COMMERCIAL RICE")
        assert well_milled is not None
        assert well_milled.price_prevailing == Decimal("48.83")
        assert well_milled.unit == "kg"

        egg = _get_item(report, "Chicken Egg (White, Medium)")
        assert egg is not None
        assert egg.price_prevailing == Decimal("8.19")
        assert egg.unit == "pc"
        assert egg.sub_category == "POULTRY PRODUCTS"
        assert egg.group_category == "EGG"

        red_onion = _get_item(report, "Red Onion, Local")
        assert red_onion is not None
        assert red_onion.price_prevailing == Decimal("135.63")
        assert red_onion.unit == "kg"

        # n/a handling
        bonito = _get_item(report, "Bonito (Frigate Tuna)")
        assert bonito is not None
        assert bonito.price_prevailing is None
        assert bonito.is_available is False

    def test_parse_nov_23(self):
        report = parse_dpi_pdf(PDF_NOV_23)
        assert report.report_date == date(2026, 11, 23)
        assert report.region_code == "NCR"
        assert len(report.covered_markets) == 30
        assert len(report.items) >= 100

        basmati = _get_item(report, "Basmati")
        assert basmati is not None
        assert basmati.price_prevailing == Decimal("196.00")

        red_onion = _get_item(report, "Red Onion, Local")
        assert red_onion is not None
        assert red_onion.price_prevailing == Decimal("137.74")

    def test_parse_nov_24(self):
        report = parse_dpi_pdf(PDF_NOV_24)
        assert report.report_date == date(2026, 11, 24)
        assert report.region_code == "NCR"
        assert len(report.covered_markets) == 31
        assert len(report.items) >= 100

        basmati = _get_item(report, "Basmati")
        assert basmati is not None
        assert basmati.price_prevailing == Decimal("196.00")

        red_onion = _get_item(report, "Red Onion, Local")
        assert red_onion is not None
        assert red_onion.price_prevailing == Decimal("141.32")

    def test_cross_day_canonical_key_stability(self):
        r22 = parse_dpi_pdf(PDF_NOV_22)
        r23 = parse_dpi_pdf(PDF_NOV_23)
        r24 = parse_dpi_pdf(PDF_NOV_24)

        keys22 = {i.canonical_key for i in r22.items}
        keys23 = {i.canonical_key for i in r23.items}
        keys24 = {i.canonical_key for i in r24.items}

        assert keys22 == keys23, f"Mismatch 22 vs 23: {keys22 ^ keys23}"
        assert keys23 == keys24, f"Mismatch 23 vs 24: {keys23 ^ keys24}"

    def test_units_derivation(self):
        report = parse_dpi_pdf(PDF_NOV_22)

        # Cooking oil -> 'L'
        palm_oil = _get_item(report, "Cooking Oil (Palm)")
        assert palm_oil is not None
        assert palm_oil.unit == "L"

        # Eggs -> 'pc'
        egg = _get_item(report, "Chicken Egg (White, Large)")
        assert egg is not None
        assert egg.unit == "pc"

        # Agri-fishery / salt / sugar -> 'kg'
        sugar = _get_item(report, "Sugar (Refined)")
        assert sugar is not None
        assert sugar.unit == "kg"

        pork = _get_item(report, "Belly (Liempo), Local")
        assert pork is not None
        assert pork.unit == "kg"


class TestReportDateReconciliation:
    """Header date vs file-name date cross-check."""

    def test_misdated_header_uses_filename_date(self):
        report = parse_dpi_pdf(PDF_NOV_25_MISDATED)
        assert report.header_date == date(2026, 11, 13)
        assert report.report_date == date(2026, 11, 25)
        assert len(report.date_warnings) == 1
        assert "2026-11-25" in report.date_warnings[0]

    def test_consistent_fixtures_have_no_warnings(self):
        for pdf in (PDF_NOV_22, PDF_NOV_23, PDF_NOV_24):
            report = parse_dpi_pdf(pdf)
            assert report.date_warnings == [], pdf.name
            assert report.header_date == report.report_date

    def test_filename_date_extraction(self):
        assert _extract_filename_date("Daily-Price-Index-November-25-2026.pdf") == date(
            2026, 11, 25
        )
        assert _extract_filename_date("November-24-2026-DPI-AFC.pdf") == date(2026, 11, 24)
        assert _extract_filename_date("Daily-Price-Index-Nov-5-2026_ab12cd34.pdf") == date(
            2026, 11, 5
        )
        assert _extract_filename_date("dpi_ab12cd34.pdf") is None
        # Weekly ranges aren't a single date
        assert _extract_filename_date("Weekly-Average-Prices-November-24-29-2025.pdf") is None

    def test_no_filename_date_keeps_header_but_flags_weekday(self):
        d, warnings = _reconcile_report_date(date(2026, 11, 13), "Wednesday", "dpi_ab12cd34.pdf")
        assert d == date(2026, 11, 13)
        assert len(warnings) == 1 and "Friday" in warnings[0]

        d, warnings = _reconcile_report_date(date(2026, 11, 13), "Friday", "dpi_ab12cd34.pdf")
        assert d == date(2026, 11, 13)
        assert warnings == []
