"""Validation guardrails and drift annotation for parsed DPI reports."""

from __future__ import annotations

import logging
from decimal import Decimal

from da_price_index.config import settings
from da_price_index.models import ParsedReport

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Raised when a parsed report fails validation checks."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s): {'; '.join(errors)}")


class ValidationWarning:
    """A non-fatal validation warning."""

    def __init__(
        self,
        message: str,
        commodity_key: str | None = None,
        pct_change: float | None = None,
    ) -> None:
        self.message = message
        self.commodity_key = commodity_key
        self.pct_change = pct_change

    def __repr__(self) -> str:
        return f"ValidationWarning({self.message!r})"


def annotate_drift(
    report: ParsedReport,
    prev_prices: dict[str, Decimal],
    threshold: float = 0.5,
) -> list[ValidationWarning]:
    """Annotate row items with signed day-over-day price drift and set drift flags.

    Parameters
    ----------
    report : ParsedReport
        The parsed report whose items will be annotated.
    prev_prices : dict[str, Decimal]
        Map of canonical_key -> previous prevailing price.
    threshold : float
        Absolute percentage drift threshold above which drift is flagged (default 0.5 = 50%).

    Returns
    -------
    list[ValidationWarning]
        List of drift warnings for items exceeding the threshold.
    """
    warnings: list[ValidationWarning] = []
    if not prev_prices:
        return warnings

    for item in report.items:
        if item.price_prevailing is None:
            continue

        prev_price = prev_prices.get(item.canonical_key)
        if prev_price is None or prev_price <= 0:
            continue

        pct_change = float((item.price_prevailing - prev_price) / prev_price)
        item.drift_pct = Decimal(str(round(pct_change, 4)))

        if abs(pct_change) > threshold:
            item.is_drift_flagged = True
            msg = (
                f"Price drift {pct_change:+.1%} for {item.commodity_name}: "
                f"{prev_price} → {item.price_prevailing}"
            )
            warnings.append(
                ValidationWarning(
                    message=msg,
                    commodity_key=item.canonical_key,
                    pct_change=pct_change,
                )
            )

    return warnings


def validate_report(
    report: ParsedReport,
    *,
    min_rows: int | None = None,
    max_price: float | None = None,
) -> None:
    """Validate a parsed report for schema compliance and sanity.

    Pure assertion check: never mutates the report. Drift annotation is a
    separate step (see `annotate_drift`).

    Raises
    ------
    ValidationError
        If hard validation checks fail (missing metadata, row count, negative/outlier prices).
    """
    errors: list[str] = []

    min_rows = min_rows if min_rows is not None else settings.min_expected_rows
    max_price = max_price if max_price is not None else settings.max_price_threshold

    # 1. Schema & completeness
    if not report.report_date:
        errors.append("Missing report_date")

    if not report.covered_markets:
        errors.append("No covered markets parsed from footer")

    row_count = len(report.items)
    if row_count < min_rows:
        errors.append(f"Row count {row_count} below minimum threshold {min_rows}")

    # 2. Price sanity
    for item in report.items:
        if item.price_prevailing is not None:
            if item.price_prevailing < 0:
                errors.append(
                    f"Negative price {item.price_prevailing} for "
                    f"{item.commodity_name} ({item.canonical_key})"
                )
            if item.price_prevailing > Decimal(str(max_price)):
                errors.append(
                    f"Price {item.price_prevailing} exceeds max threshold {max_price} for "
                    f"{item.commodity_name} ({item.canonical_key})"
                )

    if errors:
        raise ValidationError(errors)
