"""Exporters for parsed DPI reports and commodity prices.

Supports JSON and CSV formats for downstream e-commerce benchmarking
and HTTP API streaming.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from da_price_index.models import ParsedReport


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder supporting Decimal and date types."""

    def default(self, obj: object) -> object:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, date):
            return obj.isoformat()
        return super().default(obj)


def export_to_json(report: ParsedReport, output_path: str | Path | None = None) -> str:
    """Export a ParsedReport as formatted JSON."""
    data = report.model_dump()
    json_str = json.dumps(data, indent=2, cls=DecimalEncoder)

    if output_path is not None:
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json_str, encoding="utf-8")

    return json_str


def export_to_csv(report: ParsedReport, output_path: str | Path | None = None) -> str:
    """Export the commodity price rows of a ParsedReport as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        "report_date",
        "region_code",
        "top_category",
        "sub_category",
        "group_category",
        "commodity_name",
        "specification",
        "unit",
        "price_prevailing",
        "is_available",
        "raw_price_text",
        "canonical_key",
        "drift_flagged",
        "drift_pct",
        "page_number",
        "row_order",
    ]
    writer.writerow(headers)

    for item in report.items:
        writer.writerow([
            report.report_date.isoformat(),
            report.region_code,
            item.top_category,
            item.sub_category or "",
            item.group_category or "",
            item.commodity_name,
            item.specification,
            item.unit,
            str(item.price_prevailing) if item.price_prevailing is not None else "",
            item.is_available,
            item.raw_price_text,
            item.canonical_key,
            item.is_drift_flagged,
            str(item.drift_pct) if item.drift_pct is not None else "",
            item.page_number,
            item.row_order,
        ])

    csv_str = output.getvalue()
    if output_path is not None:
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(csv_str, encoding="utf-8", newline="")

    return csv_str


def prices_to_csv(rows: list[dict[str, Any]]) -> str:
    """Format a list of database-fetched price dictionaries as an RFC 4180 CSV string."""
    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        "report_date",
        "commodity_id",
        "commodity_name",
        "top_category",
        "sub_category",
        "group_category",
        "specification",
        "unit",
        "price_prevailing",
        "is_available",
        "drift_flagged",
        "drift_pct",
        "raw_price_text",
    ]
    writer.writerow(headers)

    for r in rows:
        report_d = r.get("report_date")
        if isinstance(report_d, date):
            report_d_str = report_d.isoformat()
        else:
            report_d_str = str(report_d or "")

        quality = r.get("quality") or {}
        drift_flagged = quality.get("drift_flagged", False)
        drift_pct = quality.get("drift_pct")
        drift_pct_str = f"{drift_pct:.4f}" if drift_pct is not None else ""

        price_prev = r.get("price_prevailing")
        price_str = str(price_prev) if price_prev is not None else ""

        writer.writerow([
            report_d_str,
            r.get("commodity_id", ""),
            r.get("commodity_name", ""),
            r.get("top_category", ""),
            r.get("sub_category", "") or "",
            r.get("group_category", "") or "",
            r.get("specification", "") or "",
            r.get("unit", ""),
            price_str,
            r.get("is_available", True),
            drift_flagged,
            drift_pct_str,
            r.get("raw_price_text", "") or "",
        ])

    return output.getvalue()
