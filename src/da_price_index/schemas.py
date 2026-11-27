"""Pydantic schemas for the DA Price Index HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class CommodityOut(BaseModel):
    """Commodity catalog dimension item."""

    commodity_id: int
    top_category: str
    sub_category: str | None = None
    group_category: str | None = None
    commodity_name: str
    specification: str = ""
    unit: str
    canonical_key: str
    is_active: bool

    model_config = {"from_attributes": True}


class PriceQualityOut(BaseModel):
    """Quality and drift metadata for a commodity price."""

    drift_flagged: bool = False
    drift_pct: float | None = None


class LatestPriceItemOut(BaseModel):
    """Latest prevailing price reference record for a single commodity."""

    price_id: int
    commodity_id: int
    commodity_name: str
    top_category: str
    sub_category: str | None = None
    group_category: str | None = None
    specification: str = ""
    unit: str
    report_date: date
    price_prevailing: Decimal | None = None
    is_available: bool = True
    raw_price_text: str = ""
    quality: PriceQualityOut = Field(default_factory=PriceQualityOut)


class LatestPricesResponse(BaseModel):
    """Response envelope for latest prevailing commodity prices."""

    region_code: str
    as_of_date: date
    total_commodities: int
    items: list[LatestPriceItemOut]


class HistoricalPriceItemOut(BaseModel):
    """Historical time-series price record for a commodity."""

    price_id: int
    commodity_id: int
    commodity_name: str
    top_category: str
    sub_category: str | None = None
    group_category: str | None = None
    specification: str = ""
    unit: str
    report_date: date
    price_prevailing: Decimal | None = None
    is_available: bool = True
    raw_price_text: str = ""
    quality: PriceQualityOut = Field(default_factory=PriceQualityOut)


class ReportProvenanceOut(BaseModel):
    """Provenance and audit metadata for a specific ingested DPI report."""

    report_id: int
    region_code: str
    region_name: str
    report_date: date
    file_name: str
    file_sha256: str
    covered_markets: list[str] = Field(default_factory=list)
    covered_markets_cnt: int = 0
    parser_version: str
    parsed_at: datetime


class PriceSummaryItemOut(BaseModel):
    """Price band for one commodity over the summary window."""

    commodity_id: int
    commodity_name: str
    top_category: str
    sub_category: str | None = None
    group_category: str | None = None
    specification: str = ""
    unit: str
    observations: int
    price_min: Decimal
    price_max: Decimal
    price_median: Decimal
    latest_price: Decimal
    first_date: date
    last_date: date


class PriceSummaryResponse(BaseModel):
    """Response envelope for trailing-window price summaries."""

    region_code: str
    days: int
    window_start: date
    window_end: date
    total_commodities: int
    items: list[PriceSummaryItemOut]
