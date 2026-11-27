"""FastAPI application for the DA Price Index HTTP reference service.

Provides reference lookups for prevailing retail commodity prices,
time-series history, and report provenance, serving JSON and CSV directly
from the relational database.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from da_price_index.config import settings
from da_price_index.db import DatabaseRepository
from da_price_index.exporters import prices_to_csv
from da_price_index.ratelimit import RateLimiter, RateLimitMiddleware, limiter_from_settings
from da_price_index.schemas import (
    CommodityOut,
    HistoricalPriceItemOut,
    LatestPricesResponse,
    PriceSummaryResponse,
    ReportProvenanceOut,
)


def create_app(
    repo: DatabaseRepository | None = None,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    """Application factory for the DA Price Index API.

    `rate_limiter` overrides the settings-based limiter (mainly for tests).
    """
    db_repo = repo or DatabaseRepository()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Ensure the schema exists so the API serves empty results (not 500s)
        # if it boots before the first scheduled sync has run.
        db_repo.create_tables()
        yield

    app = FastAPI(
        title="DA Price Index Reference API",
        description="Daily Prevailing Retail Price Monitoring and Commodity Reference Service",
        version=settings.parser_version,
        lifespan=lifespan,
    )

    # Added before CORS so CORS stays outermost: 429 responses still carry
    # CORS headers and preflight OPTIONS requests aren't counted.
    limiter = rate_limiter
    if limiter is None and settings.rate_limit_enabled:
        limiter = limiter_from_settings(
            settings.rate_limit_per_minute,
            settings.rate_limit_per_hour,
            settings.rate_limit_per_day,
        )
    if limiter is not None:
        app.add_middleware(
            RateLimitMiddleware,
            limiter=limiter,
            ip_header=settings.client_ip_header,
        )

    # Public, read-only data: any origin may read it, no cookies/credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["*"],
    )

    def get_repo() -> DatabaseRepository:
        return db_repo

    @app.get("/health", tags=["System"])
    def health_check(repository: DatabaseRepository = Depends(get_repo)) -> dict[str, Any]:
        """Health check and latest report info."""
        latest = repository.get_latest_report_info("NCR")
        latest_data = None
        if latest is not None:
            latest_data = {
                "report_id": latest[0],
                "parser_version": latest[1],
                "report_date": latest[2].isoformat(),
            }
        return {
            "status": "ok",
            "service": "DAPriceScraper",
            "version": settings.parser_version,
            "latest_report": latest_data,
        }

    @app.get("/commodities", response_model=list[CommodityOut], tags=["Catalog"])
    def list_commodities(
        category: str | None = Query(None, description="Filter by top category (e.g. Meat, Rice)"),
        search: str | None = Query(None, description="Search term for name or specification"),
        is_active: bool = Query(True, description="Filter by active status"),
        repository: DatabaseRepository = Depends(get_repo),
    ) -> list[CommodityOut]:
        """List canonical commodities in the catalog."""
        commodities = repository.get_commodities(
            top_category=category,
            search=search,
            is_active=is_active,
        )
        return [CommodityOut.model_validate(c) for c in commodities]

    @app.get(
        "/prices/latest",
        response_model=LatestPricesResponse,
        tags=["Prices"],
        responses={
            200: {
                "description": "Latest prices in JSON or RFC 4180 CSV",
                "content": {
                    "application/json": {},
                    "text/csv": {},
                },
            },
            304: {"description": "Data has not changed based on ETag"},
        },
    )
    def get_latest_prices(
        request: Request,
        region: str = Query("NCR", description="Region code (e.g. NCR)"),
        commodity_id: list[int] | None = Query(
            None, description="Optional filter by commodity ID(s)"
        ),
        available_only: bool = Query(
            False, description="Filter out unavailable ('N/A') commodities"
        ),
        format: str | None = Query(None, description="Format: 'json' or 'csv'"),
        repository: DatabaseRepository = Depends(get_repo),
    ) -> Response:
        """Fetch latest known prevailing retail price per commodity."""
        latest_report_id, parser_ver, latest_date, rows = repository.get_latest_known_prices(
            region_code=region,
            commodity_ids=commodity_id,
            available_only=available_only,
        )

        if latest_report_id is None or latest_date is None:
            raise HTTPException(
                status_code=404, detail=f"No price reports found for region '{region}'"
            )

        # Determine if CSV is requested via query param or Accept header
        accept_header = request.headers.get("accept", "").lower()
        is_csv = (format == "csv") or ("text/csv" in accept_header)

        # Compute robust ETag
        comm_ids_key = ",".join(str(i) for i in sorted(commodity_id)) if commodity_id else "all"
        etag_seed = (
            f"{latest_report_id}:{parser_ver}:{comm_ids_key}:{is_csv}:{available_only}:{region}"
        )
        etag_val = f'W/"{hashlib.sha256(etag_seed.encode("utf-8")).hexdigest()[:16]}"'

        headers = {
            "ETag": etag_val,
            "Cache-Control": (
                f"public, max-age={settings.cache_max_age_seconds}, "
                "stale-while-revalidate=86400"
            ),
        }

        # Check 304 Not Modified
        client_etag = request.headers.get("if-none-match")
        if client_etag and client_etag == etag_val:
            return Response(status_code=304, headers=headers)

        if is_csv:
            csv_content = prices_to_csv(rows)
            headers["Content-Disposition"] = (
                f'inline; filename="da_prices_{region}_{latest_date}.csv"'
            )
            return Response(content=csv_content, media_type="text/csv", headers=headers)

        envelope = LatestPricesResponse(
            region_code=region,
            as_of_date=latest_date,
            total_commodities=len(rows),
            items=rows,  # type: ignore[arg-type]
        )
        return Response(
            content=envelope.model_dump_json(),
            media_type="application/json",
            headers=headers,
        )

    @app.get(
        "/prices/summary",
        response_model=PriceSummaryResponse,
        tags=["Prices"],
        responses={304: {"description": "Data has not changed based on ETag"}},
    )
    def get_price_summary(
        request: Request,
        days: int = Query(
            30,
            ge=1,
            le=366,
            description="Trailing window in days, ending on the latest report date",
        ),
        region: str = Query("NCR", description="Region code (e.g. NCR)"),
        commodity_id: list[int] | None = Query(
            None, description="Optional filter by commodity ID(s)"
        ),
        repository: DatabaseRepository = Depends(get_repo),
    ) -> Response:
        """Min / max / median prevailing price per commodity over the last `days` days.

        One call returns a price band for every commodity, instead of one
        `/prices` history call per commodity.
        """
        latest = repository.get_latest_report_info(region)
        if latest is None:
            raise HTTPException(
                status_code=404, detail=f"No price reports found for region '{region}'"
            )

        comm_ids_key = ",".join(str(i) for i in sorted(commodity_id)) if commodity_id else "all"
        etag_seed = f"summary:{latest[0]}:{latest[1]}:{latest[2]}:{days}:{comm_ids_key}:{region}"
        etag_val = f'W/"{hashlib.sha256(etag_seed.encode("utf-8")).hexdigest()[:16]}"'
        headers = {
            "ETag": etag_val,
            "Cache-Control": (
                f"public, max-age={settings.cache_max_age_seconds}, "
                "stale-while-revalidate=86400"
            ),
        }
        if request.headers.get("if-none-match") == etag_val:
            return Response(status_code=304, headers=headers)

        window_start, window_end, rows = repository.get_price_summary(
            days=days, region_code=region, commodity_ids=commodity_id
        )
        envelope = PriceSummaryResponse(
            region_code=region,
            days=days,
            window_start=window_start,  # type: ignore[arg-type]
            window_end=window_end,  # type: ignore[arg-type]
            total_commodities=len(rows),
            items=rows,  # type: ignore[arg-type]
        )
        return Response(
            content=envelope.model_dump_json(),
            media_type="application/json",
            headers=headers,
        )

    @app.get(
        "/prices",
        response_model=list[HistoricalPriceItemOut],
        tags=["Prices"],
        responses={
            200: {
                "description": "Historical time-series prices in JSON or RFC 4180 CSV",
                "content": {
                    "application/json": {},
                    "text/csv": {},
                },
            }
        },
    )
    def get_price_history(
        request: Request,
        commodity_id: int = Query(..., description="Target commodity ID"),
        from_date: date | None = Query(None, alias="from", description="Start date (YYYY-MM-DD)"),
        to_date: date | None = Query(None, alias="to", description="End date (YYYY-MM-DD)"),
        region: str = Query("NCR", description="Region code"),
        format: str | None = Query(None, description="Format: 'json' or 'csv'"),
        repository: DatabaseRepository = Depends(get_repo),
    ) -> Response:
        """Fetch historical prevailing price time series for a specific commodity."""
        rows = repository.get_price_history(
            commodity_id=commodity_id,
            from_date=from_date,
            to_date=to_date,
            region_code=region,
        )

        # Empty history is valid for a known commodity; an unknown ID is a 404.
        if not rows and repository.get_commodity(commodity_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"Commodity with id {commodity_id} not found",
            )

        accept_header = request.headers.get("accept", "").lower()
        is_csv = (format == "csv") or ("text/csv" in accept_header)

        if is_csv:
            csv_content = prices_to_csv(rows)
            headers = {
                "Content-Disposition": f'inline; filename="commodity_{commodity_id}_prices.csv"'
            }
            return Response(content=csv_content, media_type="text/csv", headers=headers)

        # JSON response
        items = [HistoricalPriceItemOut.model_validate(r) for r in rows]
        json_data = json.dumps([item.model_dump(mode="json") for item in items])
        return Response(content=json_data, media_type="application/json")

    @app.get(
        "/reports/{report_date}",
        response_model=ReportProvenanceOut,
        tags=["Reports"],
    )
    def get_report(
        report_date: date,
        region: str = Query("NCR", description="Region code (default: NCR)"),
        repository: DatabaseRepository = Depends(get_repo),
    ) -> ReportProvenanceOut:
        """Fetch provenance metadata and covered markets for a specific report date."""
        report = repository.get_report_by_date(report_date=report_date, region_code=region)
        if report is None:
            raise HTTPException(
                status_code=404,
                detail=f"No report found for date '{report_date}' in region '{region}'",
            )

        markets = []
        if report.covered_markets:
            try:
                markets = json.loads(report.covered_markets)
            except Exception:
                markets = []

        return ReportProvenanceOut(
            report_id=report.report_id,
            region_code=report.region_code,
            region_name=report.region_name,
            report_date=report.report_date,
            file_name=report.file_name,
            file_sha256=report.file_sha256,
            covered_markets=markets,
            covered_markets_cnt=report.covered_markets_cnt or len(markets),
            parser_version=report.parser_version,
            parsed_at=report.parsed_at,
        )

    return app


# Module-level default application instance
app = create_app()
