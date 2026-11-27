"""Pydantic domain models for parsed DPI price reports."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


def _make_canonical_key(
    top_category: str,
    sub_category: str | None,
    group_category: str | None,
    commodity_name: str,
    specification: str,
    unit: str,
) -> str:
    """Build a deterministic canonical key from commodity identity fields.

    The key is lowercase, collapsed-whitespace, with select punctuation removed,
    so minor line-wrap differences across PDF days still resolve identically.
    """
    parts = [
        top_category,
        sub_category or "",
        group_category or "",
        commodity_name,
        specification,
        unit,
    ]
    normalised = []
    for p in parts:
        s = p.lower().strip()
        # Collapse all whitespace (incl. newlines from PDF wrapping)
        s = re.sub(r"\s+", " ", s)
        # Remove stray punctuation that can vary between PDF renders
        # Keep hyphens, slashes, parens, commas — they're semantically meaningful
        normalised.append(s)
    return "|".join(normalised)


class ParsedPriceRow(BaseModel):
    """A single commodity price row extracted from a DPI PDF."""

    top_category: str
    sub_category: str | None = None
    group_category: str | None = None
    commodity_name: str
    specification: str = ""
    unit: str  # 'kg', 'pc', 'L'
    canonical_key: str = ""
    price_prevailing: Decimal | None = None
    is_available: bool = True
    raw_price_text: str = ""
    page_number: int = 0
    row_order: int = 0
    is_drift_flagged: bool = False
    drift_pct: Decimal | None = None

    @field_validator("commodity_name", "specification", mode="before")
    @classmethod
    def normalise_whitespace(cls, v: str) -> str:
        if not v:
            return v
        return re.sub(r"\s+", " ", str(v)).strip()

    def model_post_init(self, __context: object) -> None:
        if not self.canonical_key:
            self.canonical_key = _make_canonical_key(
                self.top_category,
                self.sub_category,
                self.group_category,
                self.commodity_name,
                self.specification,
                self.unit,
            )


class ParsedReport(BaseModel):
    """Full parsed output from a single DPI PDF."""

    region_code: str = "NCR"
    region_name: str = ""
    report_date: date
    # Date as printed in the PDF header; differs from report_date when the
    # header disagreed with the file name (see parser._reconcile_report_date).
    header_date: date | None = None
    date_warnings: list[str] = Field(default_factory=list)
    file_name: str = ""
    file_sha256: str = ""
    covered_markets: list[str] = Field(default_factory=list)
    items: list[ParsedPriceRow] = Field(default_factory=list)
    parser_version: str = "0.1.0"

    @property
    def covered_markets_count(self) -> int:
        return len(self.covered_markets)
