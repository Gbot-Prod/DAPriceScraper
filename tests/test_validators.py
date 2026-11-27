"""Unit tests for report validators and anomaly detection."""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal

try:
    import pytest
except ImportError:
    class _MockPytest:
        @contextmanager
        def raises(self, expected_exc):
            class ExcInfo:
                value = None
            info = ExcInfo()
            try:
                yield info
            except expected_exc as e:
                info.value = e
            else:
                raise AssertionError(f"Expected {expected_exc} was not raised")

    pytest = _MockPytest()

from da_price_index.models import ParsedPriceRow, ParsedReport
from da_price_index.validators import ValidationError, annotate_drift, validate_report


def _make_dummy_report(
    num_rows: int = 150,
    price: Decimal | None = Decimal("100.00"),
    report_date: date = date(2026, 11, 24),
    covered_markets: list[str] | None = None,
) -> ParsedReport:
    markets = covered_markets if covered_markets is not None else ["Market 1", "Market 2"]
    items = [
        ParsedPriceRow(
            top_category="CORN PRODUCTS",
            commodity_name=f"Commodity {i}",
            specification="Spec",
            unit="kg",
            price_prevailing=price,
            is_available=True,
            raw_price_text=str(price) if price else "n/a",
            page_number=1,
            row_order=i,
        )
        for i in range(1, num_rows + 1)
    ]
    return ParsedReport(
        region_code="NCR",
        region_name="National Capital Region (NCR)",
        report_date=report_date,
        file_name="dummy.pdf",
        file_sha256="abc" * 21 + "a",
        covered_markets=markets,
        items=items,
    )


class TestValidators:
    """Test validation guardrails and drift annotation."""

    def test_valid_report_passes(self):
        report = _make_dummy_report(num_rows=120)
        validate_report(report)  # must not raise

    def test_validate_report_does_not_mutate(self):
        report = _make_dummy_report(num_rows=120)
        before = report.model_dump()
        validate_report(report)
        assert report.model_dump() == before

    def test_low_row_count_rejected(self):
        report = _make_dummy_report(num_rows=50)
        with pytest.raises(ValidationError) as exc:
            validate_report(report, min_rows=100)
        assert "below minimum threshold" in str(exc.value)

    def test_empty_covered_markets_rejected(self):
        report = _make_dummy_report(covered_markets=[])
        with pytest.raises(ValidationError) as exc:
            validate_report(report)
        assert "No covered markets" in str(exc.value)

    def test_negative_price_rejected(self):
        report = _make_dummy_report(num_rows=120, price=Decimal("-10.00"))
        with pytest.raises(ValidationError) as exc:
            validate_report(report)
        assert "Negative price" in str(exc.value)

    def test_extreme_price_rejected(self):
        report = _make_dummy_report(num_rows=120, price=Decimal("15000.00"))
        with pytest.raises(ValidationError) as exc:
            validate_report(report, max_price=10000.0)
        assert "exceeds max threshold" in str(exc.value)

    def test_day_over_day_price_drift_warning(self):
        prev = _make_dummy_report(num_rows=120, price=Decimal("100.00"))
        # Price increases from 100 to 180 (> 50% change)
        curr = _make_dummy_report(num_rows=120, price=Decimal("180.00"))

        prev_prices = {
            item.canonical_key: item.price_prevailing
            for item in prev.items
            if item.price_prevailing is not None
        }
        warnings = annotate_drift(curr, prev_prices, threshold=0.5)
        assert len(warnings) == 120
        assert "Price drift" in warnings[0].message
        assert curr.items[0].is_drift_flagged is True
        assert curr.items[0].drift_pct == Decimal("0.8")

    def test_annotate_drift_signed_and_threshold(self):
        """annotate_drift must preserve sign (+ for price up, - for price down)."""
        row_up = ParsedPriceRow(
            top_category="Meat",
            commodity_name="Beef",
            unit="kg",
            price_prevailing=Decimal("300.00"),
        )
        row_down = ParsedPriceRow(
            top_category="Meat",
            commodity_name="Pork",
            unit="kg",
            price_prevailing=Decimal("150.00"),
        )
        report = ParsedReport(
            report_date=date(2026, 11, 25),
            covered_markets=["Market A"],
            items=[row_up, row_down],
        )

        prev_prices = {
            row_up.canonical_key: Decimal("200.00"),    # 300 vs 200 = +50%
            row_down.canonical_key: Decimal("200.00"),  # 150 vs 200 = -25%
        }

        # Threshold 0.3 (30%)
        warnings = annotate_drift(report, prev_prices, threshold=0.3)
        assert len(warnings) == 1  # only row_up exceeds 30%
        assert row_up.is_drift_flagged is True
        assert row_up.drift_pct == Decimal("0.5")

        assert row_down.is_drift_flagged is False
        assert row_down.drift_pct == Decimal("-0.25")
