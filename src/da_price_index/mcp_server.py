"""MCP server: lets AI assistants (Claude, Cursor, VS Code...) query DA prices.

Served over Streamable HTTP at ``/mcp`` by the FastAPI app. Tools are
read-only, wrap the same repository queries as the REST endpoints, and keep
results small so they fit comfortably in a model's context window.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from da_price_index.config import settings
from da_price_index.db import DatabaseRepository

MAX_IDS = 50
MAX_HISTORY_ROWS = 366

INSTRUCTIONS = """\
Daily prevailing retail prices of food commodities in Metro Manila (NCR) public
markets, parsed from the Philippine Department of Agriculture's Daily Price Index.
This is an unofficial community project, not affiliated with the DA.

How to use the tools:
1. Commodities are identified by numeric commodity_id. You don't know the IDs in
   advance, so call search_commodities first (e.g. "liempo", "galunggong",
   "red onion", "well-milled rice"). Use list_categories to browse.
2. Then call get_latest_prices, get_price_summary or get_price_history with those IDs.

Prices are in Philippine pesos (PHP) per the listed unit (usually kg). A
"prevailing" price is the most common price observed across surveyed markets
that day. Prices can be null when the commodity was unavailable. When you
quote prices, mention the report date and that the source is the DA's Daily
Price Index.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def _category(row: dict[str, Any]) -> str:
    parts = [row.get("top_category"), row.get("sub_category"), row.get("group_category")]
    return " > ".join(p for p in parts if p)


def _check_ids(commodity_ids: list[int]) -> list[int]:
    ids = list(dict.fromkeys(commodity_ids))
    if not ids:
        raise ToolError("Pass at least one commodity_id. Use search_commodities to find IDs.")
    if len(ids) > MAX_IDS:
        raise ToolError(f"Pass at most {MAX_IDS} commodity IDs per call.")
    return ids


def _parse_date(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ToolError(f"{name} must be a date in YYYY-MM-DD format, got {value!r}.") from e


def build_mcp_server(repo: DatabaseRepository) -> MCPServer:
    """Create the MCP server bound to a repository."""
    server = MCPServer(
        name="DAPriceScraper",
        title="Philippine DA Food Prices",
        description="Daily prevailing retail prices of food commodities in Metro Manila markets.",
        instructions=INSTRUCTIONS,
        version=settings.parser_version,
    )

    @server.tool(annotations=READ_ONLY)
    async def search_commodities(
        query: str, category: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Find commodities by name, specification or sub-category and get their commodity_id.

        Matching is case-insensitive and partial, e.g. "pork", "liempo", "galunggong",
        "onion", "rice". Optionally narrow by top category (partial match, e.g. "vegetables").
        """
        limit = max(1, min(limit, 50))
        rows = await anyio.to_thread.run_sync(
            lambda: repo.get_commodities(top_category=category, search=query)
        )
        return {
            "total_matches": len(rows),
            "commodities": [
                {
                    "commodity_id": c.commodity_id,
                    "name": c.commodity_name,
                    "specification": c.specification or None,
                    "unit": c.unit,
                    "category": " > ".join(
                        p for p in (c.top_category, c.sub_category, c.group_category) if p
                    ),
                }
                for c in rows[:limit]
            ],
        }

    @server.tool(annotations=READ_ONLY)
    async def list_categories() -> dict[str, Any]:
        """List the commodity categories (with sub-categories and counts) to browse the catalog."""
        rows = await anyio.to_thread.run_sync(lambda: repo.get_commodities())
        tree: dict[str, dict[str, int]] = {}
        for c in rows:
            subs = tree.setdefault(c.top_category, {})
            key = c.sub_category or "(none)"
            subs[key] = subs.get(key, 0) + 1
        return {
            "categories": [
                {
                    "category": top,
                    "commodities": sum(subs.values()),
                    "sub_categories": [s for s in sorted(subs) if s != "(none)"],
                }
                for top, subs in sorted(tree.items())
            ]
        }

    @server.tool(annotations=READ_ONLY)
    async def get_latest_prices(commodity_ids: list[int], region: str = "NCR") -> dict[str, Any]:
        """Latest known prevailing price for the given commodities (up to 50 IDs).

        A commodity missing from the newest report keeps its last known price; check
        each item's report_date. day_change is the signed change vs the previous
        price as a fraction (0.12 = +12%).
        """
        ids = _check_ids(commodity_ids)
        _, _, as_of, rows = await anyio.to_thread.run_sync(
            lambda: repo.get_latest_known_prices(region_code=region, commodity_ids=ids)
        )
        if as_of is None:
            raise ToolError(f"No price reports found for region {region!r}.")
        return {
            "region": region,
            "latest_report_date": as_of.isoformat(),
            "currency": "PHP",
            "prices": [
                {
                    "commodity_id": r["commodity_id"],
                    "name": r["commodity_name"],
                    "specification": r["specification"] or None,
                    "category": _category(r),
                    "unit": r["unit"],
                    "price": _money(r["price_prevailing"]),
                    "available": r["is_available"],
                    "report_date": r["report_date"].isoformat(),
                    "day_change": r["quality"]["drift_pct"],
                    "flagged_large_change": r["quality"]["drift_flagged"],
                }
                for r in rows
            ],
            "not_found": sorted(set(ids) - {r["commodity_id"] for r in rows}),
        }

    @server.tool(annotations=READ_ONLY)
    async def get_price_summary(
        commodity_ids: list[int], days: int = 30, region: str = "NCR"
    ) -> dict[str, Any]:
        """Min, max, median and latest price per commodity over the last `days` days (1-366).

        The window ends on the latest report date. Good for "how much has X changed
        this month" or comparing price ranges. Up to 50 IDs.
        """
        ids = _check_ids(commodity_ids)
        if not 1 <= days <= 366:
            raise ToolError("days must be between 1 and 366.")
        start, end, rows = await anyio.to_thread.run_sync(
            lambda: repo.get_price_summary(days=days, region_code=region, commodity_ids=ids)
        )
        if end is None:
            raise ToolError(f"No price reports found for region {region!r}.")
        return {
            "region": region,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "currency": "PHP",
            "summaries": [
                {
                    "commodity_id": r["commodity_id"],
                    "name": r["commodity_name"],
                    "specification": r["specification"] or None,
                    "unit": r["unit"],
                    "days_observed": r["observations"],
                    "min": _money(r["price_min"]),
                    "max": _money(r["price_max"]),
                    "median": _money(r["price_median"]),
                    "latest": _money(r["latest_price"]),
                    "first_date": r["first_date"].isoformat(),
                    "last_date": r["last_date"].isoformat(),
                }
                for r in rows
            ],
            "no_data": sorted(set(ids) - {r["commodity_id"] for r in rows}),
        }

    @server.tool(annotations=READ_ONLY)
    async def get_price_history(
        commodity_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        region: str = "NCR",
    ) -> dict[str, Any]:
        """Daily price history of one commodity, oldest first. Dates are YYYY-MM-DD.

        Returns at most the 366 most recent rows in the range; use start_date to page back.
        """
        start = _parse_date(start_date, "start_date")
        end = _parse_date(end_date, "end_date")

        def query() -> tuple[Any, list[dict[str, Any]]]:
            commodity = repo.get_commodity(commodity_id)
            if commodity is None:
                return None, []
            return commodity, repo.get_price_history(commodity_id, start, end, region)

        commodity, rows = await anyio.to_thread.run_sync(query)
        if commodity is None:
            raise ToolError(
                f"No commodity with id {commodity_id}. Use search_commodities to find IDs."
            )
        truncated = len(rows) > MAX_HISTORY_ROWS
        rows = rows[-MAX_HISTORY_ROWS:]
        return {
            "commodity_id": commodity.commodity_id,
            "name": commodity.commodity_name,
            "specification": commodity.specification or None,
            "unit": commodity.unit,
            "region": region,
            "currency": "PHP",
            "truncated": truncated,
            "history": [
                {
                    "date": r["report_date"].isoformat(),
                    "price": _money(r["price_prevailing"]),
                    "available": r["is_available"],
                }
                for r in rows
            ],
        }

    @server.tool(annotations=READ_ONLY)
    async def get_report_info(
        report_date: str | None = None, region: str = "NCR"
    ) -> dict[str, Any]:
        """Source details for a day's prices: the DA file, its checksum and the markets surveyed.

        Omit report_date for the latest report. Use this to cite where a price came from.
        """
        wanted = _parse_date(report_date, "report_date")

        def query() -> Any:
            target = wanted
            if target is None:
                latest = repo.get_latest_report_info(region)
                if latest is None:
                    return None
                target = latest[2]
            return repo.get_report_by_date(target, region)

        report = await anyio.to_thread.run_sync(query)
        if report is None:
            raise ToolError(
                f"No report found for {report_date or 'the latest date'} in region {region!r}."
            )
        markets: list[str] = []
        if isinstance(report.covered_markets, list):
            markets = report.covered_markets
        elif report.covered_markets:
            try:
                markets = json.loads(report.covered_markets)
            except (TypeError, ValueError):
                markets = []
        return {
            "report_date": report.report_date.isoformat(),
            "region": report.region_code,
            "region_name": report.region_name,
            "source": "Philippine Department of Agriculture, Daily Price Index",
            "source_url": settings.portal_url,
            "file_name": report.file_name,
            "file_sha256": report.file_sha256,
            "markets_surveyed": markets,
            "market_count": report.covered_markets_cnt or len(markets),
        }

    return server
