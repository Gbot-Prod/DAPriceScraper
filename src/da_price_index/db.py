"""SQLAlchemy models and database repository with idempotent upserts.

Supports both PostgreSQL and SQLite (in-memory or file-based).
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    or_,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from da_price_index.config import settings
from da_price_index.models import ParsedReport

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy domain models."""
    pass


class PriceReport(Base):
    """Metadata and provenance for an ingested DPI PDF report."""

    __tablename__ = "price_reports"

    report_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region_code: Mapped[str] = mapped_column(String(32), default="NCR", nullable=False)
    region_name: Mapped[str] = mapped_column(String(128), nullable=False)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    covered_markets: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded array
    covered_markets_cnt: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    parsed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)

    prices: Mapped[list[DailyCommodityPrice]] = relationship(
        "DailyCommodityPrice",
        back_populates="report",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("region_code", "report_date", name="uq_price_reports_region_date"),
    )


class Commodity(Base):
    """Canonical commodity dimension table deduplicated across all report days."""

    __tablename__ = "commodities"

    commodity_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    top_category: Mapped[str] = mapped_column(String(128), nullable=False)
    sub_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    group_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    commodity_name: Mapped[str] = mapped_column(String(255), nullable=False)
    specification: Mapped[str] = mapped_column(Text, default="", nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    prices: Mapped[list[DailyCommodityPrice]] = relationship(
        "DailyCommodityPrice",
        back_populates="commodity",
    )
    aliases: Mapped[list[CommodityAlias]] = relationship(
        "CommodityAlias",
        back_populates="commodity",
        cascade="all, delete-orphan",
    )


class CommodityAlias(Base):
    """Alias mapping for DA commodity renames or layout variations."""

    __tablename__ = "commodity_aliases"

    alias_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    commodity_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("commodities.commodity_id", ondelete="CASCADE"),
        nullable=False,
    )
    canonical_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    alias_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    commodity: Mapped[Commodity] = relationship("Commodity", back_populates="aliases")

    __table_args__ = (
        Index("idx_commodity_aliases_key", "canonical_key"),
    )


class DailyCommodityPrice(Base):
    """Daily prevailing retail prices fact table."""

    __tablename__ = "daily_commodity_prices"

    price_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("price_reports.report_id", ondelete="CASCADE"),
        nullable=False,
    )
    commodity_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("commodities.commodity_id"),
        nullable=False,
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    price_prevailing: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    raw_price_text: Mapped[str] = mapped_column(String(64), nullable=False)
    page_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    row_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_drift_flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    drift_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)

    report: Mapped[PriceReport] = relationship("PriceReport", back_populates="prices")
    commodity: Mapped[Commodity] = relationship("Commodity", back_populates="prices")

    __table_args__ = (
        UniqueConstraint("report_id", "commodity_id", name="uq_prices_report_commodity"),
        Index("idx_daily_prices_date_commodity", "report_date", "commodity_id"),
    )


# ---------------------------------------------------------------------------
# Database Repository & Upsert Logic
# ---------------------------------------------------------------------------

class JobRun(Base):
    """Outcome log for scheduled jobs, used to detect consecutive failures."""

    __tablename__ = "job_runs"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_job_runs_job_name_run_id", "job_name", "run_id"),)


def normalize_db_url(url: str) -> str:
    """Route Postgres URLs to the installed psycopg (v3) driver.

    Hosts like Railway hand out `postgres://` / `postgresql://` URLs, which
    SQLAlchemy maps to psycopg2 — not a dependency of this project.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class DatabaseRepository:
    """Encapsulates database operations, table creation, and transactional upserts."""

    def __init__(self, db_url: str | None = None, engine: Engine | None = None) -> None:
        self.db_url = normalize_db_url(db_url or settings.db_url)
        if engine is not None:
            self.engine = engine
        else:
            connect_args: dict[str, Any] = {}
            if self.db_url.startswith("sqlite"):
                connect_args["check_same_thread"] = False
                if ":memory:" not in self.db_url:
                    parsed_path = self.db_url.replace("sqlite:///", "").replace("sqlite://", "")
                    if parsed_path:
                        Path(parsed_path).parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(self.db_url, connect_args=connect_args)

        self.session_factory = sessionmaker(bind=self.engine)

    def close(self) -> None:
        """Dispose the engine and release file locks/connections."""
        self.engine.dispose()

    def create_tables(self) -> None:
        """Create all database tables if they do not exist."""
        Base.metadata.create_all(self.engine)

    def get_session(self) -> Session:
        """Return a new SQLAlchemy Session."""
        return self.session_factory()

    def get_previous_prices(self, region_code: str, before_date: date) -> dict[str, Decimal]:
        """Fetch prevailing prices for the most recent report date prior to `before_date`."""
        with self.get_session() as session:
            # 1. Find the latest report date strictly before `before_date`
            latest_prev_date = session.scalar(
                select(PriceReport.report_date)
                .where(
                    PriceReport.region_code == region_code,
                    PriceReport.report_date < before_date,
                )
                .order_by(PriceReport.report_date.desc())
                .limit(1)
            )
            if latest_prev_date is None:
                return {}

            # 2. Query commodity canonical keys and prevailing prices for that date
            stmt = (
                select(Commodity.canonical_key, DailyCommodityPrice.price_prevailing)
                .join(Commodity, DailyCommodityPrice.commodity_id == Commodity.commodity_id)
                .join(PriceReport, DailyCommodityPrice.report_id == PriceReport.report_id)
                .where(
                    PriceReport.region_code == region_code,
                    PriceReport.report_date == latest_prev_date,
                    DailyCommodityPrice.price_prevailing.is_not(None),
                )
            )
            rows = session.execute(stmt).all()
            return {key: price for key, price in rows if price is not None}

    def upsert_report(
        self,
        report: ParsedReport,
        source_url: str | None = None,
    ) -> tuple[int, int, int]:
        """Atomically upsert a parsed DPI report into the database."""
        with self.get_session() as session:
            with session.begin():
                # 1. Check if report is already ingested. Match on SHA-256 first so
                #    a re-parse of the same file that corrects its date updates
                #    that row, then fall back to (region_code, report_date).
                existing_report = session.scalar(
                    select(PriceReport).where(PriceReport.file_sha256 == report.file_sha256)
                )
                if existing_report is None:
                    existing_report = session.scalar(
                        select(PriceReport).where(
                            PriceReport.region_code == report.region_code,
                            PriceReport.report_date == report.report_date,
                        )
                    )

                if existing_report is not None:
                    if (
                        existing_report.parser_version == report.parser_version
                        and existing_report.file_sha256 == report.file_sha256
                    ):
                        existing_prices_cnt = session.scalar(
                            select(func.count(DailyCommodityPrice.price_id))
                            .where(DailyCommodityPrice.report_id == existing_report.report_id)
                        )
                        if existing_prices_cnt and existing_prices_cnt > 0:
                            logger.info(
                                "Report for %s (%s) already ingested; skipping (idempotent).",
                                report.report_date,
                                report.file_name,
                            )
                            return 0, existing_report.report_id, 0

                    # Re-parsing: update report record and clear old prices
                    if existing_report.report_date != report.report_date:
                        logger.warning(
                            "Correcting report %s date %s -> %s (%s).",
                            existing_report.report_id,
                            existing_report.report_date,
                            report.report_date,
                            report.file_name,
                        )
                    existing_report.report_date = report.report_date
                    existing_report.region_code = report.region_code
                    existing_report.region_name = report.region_name
                    existing_report.file_name = report.file_name
                    existing_report.file_sha256 = report.file_sha256
                    existing_report.covered_markets = json.dumps(report.covered_markets)
                    existing_report.covered_markets_cnt = len(report.covered_markets)
                    existing_report.parser_version = report.parser_version
                    existing_report.parsed_at = datetime.now(timezone.utc)
                    if source_url:
                        existing_report.source_url = source_url

                    session.execute(
                        delete(DailyCommodityPrice).where(
                            DailyCommodityPrice.report_id == existing_report.report_id
                        )
                    )
                    db_report = existing_report
                else:
                    db_report = PriceReport(
                        region_code=report.region_code,
                        region_name=report.region_name,
                        report_date=report.report_date,
                        source_url=source_url,
                        file_name=report.file_name,
                        file_sha256=report.file_sha256,
                        covered_markets=json.dumps(report.covered_markets),
                        covered_markets_cnt=len(report.covered_markets),
                        parser_version=report.parser_version,
                        parsed_at=datetime.now(timezone.utc),
                    )
                    session.add(db_report)
                    session.flush()

                # 2. Commodity resolution: Exact key -> Alias exact key -> Create new
                keys_in_report = list({item.canonical_key for item in report.items})
                existing_commodities = session.scalars(
                    select(Commodity).where(Commodity.canonical_key.in_(keys_in_report))
                ).all()

                commodity_id_map: dict[str, int] = {
                    c.canonical_key: c.commodity_id for c in existing_commodities
                }

                # Check aliases for unresolved keys
                unresolved_keys = [k for k in keys_in_report if k not in commodity_id_map]
                if unresolved_keys:
                    aliases = session.scalars(
                        select(CommodityAlias).where(CommodityAlias.canonical_key.in_(unresolved_keys))
                    ).all()
                    for alias in aliases:
                        commodity_id_map[alias.canonical_key] = alias.commodity_id

                new_commodities = 0
                for item in report.items:
                    if item.canonical_key not in commodity_id_map:
                        new_comm = Commodity(
                            top_category=item.top_category,
                            sub_category=item.sub_category,
                            group_category=item.group_category,
                            commodity_name=item.commodity_name,
                            specification=item.specification,
                            unit=item.unit,
                            canonical_key=item.canonical_key,
                            is_active=True,
                            created_at=datetime.now(timezone.utc),
                        )
                        session.add(new_comm)
                        session.flush()
                        commodity_id_map[item.canonical_key] = new_comm.commodity_id
                        new_commodities += 1

                # 3. Bulk-insert daily commodity prices
                price_records = [
                    DailyCommodityPrice(
                        report_id=db_report.report_id,
                        commodity_id=commodity_id_map[item.canonical_key],
                        report_date=report.report_date,
                        price_prevailing=item.price_prevailing,
                        is_available=item.is_available,
                        raw_price_text=item.raw_price_text,
                        page_number=item.page_number,
                        row_order=item.row_order,
                        is_drift_flagged=item.is_drift_flagged,
                        drift_pct=item.drift_pct,
                    )
                    for item in report.items
                ]
                session.add_all(price_records)
                session.flush()

                return new_commodities, db_report.report_id, len(price_records)

    def get_latest_report_info(self, region_code: str = "NCR") -> tuple[int, str, date] | None:
        """Return (report_id, parser_version, report_date) for latest report."""
        with self.get_session() as session:
            stmt = (
                select(PriceReport.report_id, PriceReport.parser_version, PriceReport.report_date)
                .where(PriceReport.region_code == region_code)
                .order_by(PriceReport.report_date.desc())
                .limit(1)
            )
            row = session.execute(stmt).first()
            if row is None:
                return None
            return row[0], row[1], row[2]

    def get_price_summary(
        self,
        days: int = 30,
        region_code: str = "NCR",
        commodity_ids: list[int] | None = None,
    ) -> tuple[date | None, date | None, list[dict[str, Any]]]:
        """Min / max / median prevailing price per commodity over a trailing window.

        The window is the `days` calendar days ending on the latest report date
        for the region (not today), so a late or missing DA upload doesn't
        shrink the window. Unavailable / N/A prices are excluded.

        Returns (window_start, window_end, rows); window dates are None when the
        region has no reports.
        """
        latest = self.get_latest_report_info(region_code)
        if latest is None:
            return None, None, []
        window_end = latest[2]
        window_start = window_end - timedelta(days=days - 1)

        with self.get_session() as session:
            stmt = (
                select(
                    Commodity.commodity_id,
                    Commodity.commodity_name,
                    Commodity.top_category,
                    Commodity.sub_category,
                    Commodity.group_category,
                    Commodity.specification,
                    Commodity.unit,
                    DailyCommodityPrice.report_date,
                    DailyCommodityPrice.price_prevailing,
                )
                .join(Commodity, DailyCommodityPrice.commodity_id == Commodity.commodity_id)
                .join(PriceReport, DailyCommodityPrice.report_id == PriceReport.report_id)
                .where(
                    PriceReport.region_code == region_code,
                    DailyCommodityPrice.report_date >= window_start,
                    DailyCommodityPrice.report_date <= window_end,
                    DailyCommodityPrice.is_available.is_(True),
                    DailyCommodityPrice.price_prevailing.is_not(None),
                )
                .order_by(Commodity.commodity_id, DailyCommodityPrice.report_date)
            )
            if commodity_ids:
                stmt = stmt.where(Commodity.commodity_id.in_(commodity_ids))

            grouped: dict[int, dict[str, Any]] = {}
            for r in session.execute(stmt).all():
                g = grouped.get(r.commodity_id)
                if g is None:
                    g = grouped[r.commodity_id] = {
                        "commodity_id": r.commodity_id,
                        "commodity_name": r.commodity_name,
                        "top_category": r.top_category,
                        "sub_category": r.sub_category,
                        "group_category": r.group_category,
                        "specification": r.specification,
                        "unit": r.unit,
                        "_prices": [],
                        "first_date": r.report_date,
                    }
                price = Decimal(r.price_prevailing)
                g["_prices"].append(price)
                g["last_date"] = r.report_date
                g["latest_price"] = price

        results: list[dict[str, Any]] = []
        for g in grouped.values():
            prices = g.pop("_prices")
            g["observations"] = len(prices)
            g["price_min"] = min(prices)
            g["price_max"] = max(prices)
            g["price_median"] = statistics.median(prices)
            results.append(g)
        return window_start, window_end, results

    def get_commodities(
        self,
        top_category: str | None = None,
        search: str | None = None,
        is_active: bool = True,
    ) -> list[Commodity]:
        """Fetch commodities list matching filter criteria."""
        with self.get_session() as session:
            stmt = select(Commodity)
            if is_active:
                stmt = stmt.where(Commodity.is_active.is_(True))
            if top_category:
                stmt = stmt.where(Commodity.top_category.ilike(f"%{top_category.strip()}%"))
            if search:
                term = f"%{search.strip()}%"
                stmt = stmt.where(
                    or_(
                        Commodity.commodity_name.ilike(term),
                        Commodity.specification.ilike(term),
                        Commodity.sub_category.ilike(term),
                    )
                )
            stmt = stmt.order_by(
                Commodity.top_category, Commodity.commodity_name, Commodity.specification
            )
            return list(session.scalars(stmt).all())

    def get_commodity(self, commodity_id: int) -> Commodity | None:
        """Fetch a single commodity by primary key."""
        with self.get_session() as session:
            return session.get(Commodity, commodity_id)

    def get_latest_known_prices(
        self,
        region_code: str = "NCR",
        commodity_ids: list[int] | None = None,
        available_only: bool = False,
    ) -> tuple[int | None, str | None, date | None, list[dict[str, Any]]]:
        """Fetch latest known price per commodity for region."""
        with self.get_session() as session:
            latest_info = self.get_latest_report_info(region_code=region_code)
            if latest_info is None:
                return None, None, None, []
            latest_report_id, parser_ver, latest_date = latest_info

            # Row number partitioned by commodity_id ordered by report_date DESC
            rn = (
                func.row_number()
                .over(
                    partition_by=DailyCommodityPrice.commodity_id,
                    order_by=[
                        DailyCommodityPrice.report_date.desc(),
                        DailyCommodityPrice.price_id.desc(),
                    ],
                )
                .label("rn")
            )

            subq = (
                select(
                    DailyCommodityPrice.price_id,
                    DailyCommodityPrice.report_id,
                    DailyCommodityPrice.commodity_id,
                    DailyCommodityPrice.report_date,
                    DailyCommodityPrice.price_prevailing,
                    DailyCommodityPrice.is_available,
                    DailyCommodityPrice.raw_price_text,
                    DailyCommodityPrice.is_drift_flagged,
                    DailyCommodityPrice.drift_pct,
                    rn,
                )
                .join(PriceReport, DailyCommodityPrice.report_id == PriceReport.report_id)
                .where(PriceReport.region_code == region_code)
            )
            if commodity_ids:
                subq = subq.where(DailyCommodityPrice.commodity_id.in_(commodity_ids))
            if available_only:
                subq = subq.where(DailyCommodityPrice.is_available.is_(True))

            subq_alias = subq.subquery()

            stmt = (
                select(
                    subq_alias.c.price_id,
                    subq_alias.c.report_date,
                    subq_alias.c.price_prevailing,
                    subq_alias.c.is_available,
                    subq_alias.c.raw_price_text,
                    subq_alias.c.is_drift_flagged,
                    subq_alias.c.drift_pct,
                    Commodity.commodity_id,
                    Commodity.commodity_name,
                    Commodity.top_category,
                    Commodity.sub_category,
                    Commodity.group_category,
                    Commodity.specification,
                    Commodity.unit,
                )
                .join(Commodity, subq_alias.c.commodity_id == Commodity.commodity_id)
                .where(subq_alias.c.rn == 1, Commodity.is_active.is_(True))
                .order_by(Commodity.top_category, Commodity.commodity_name)
            )

            rows = session.execute(stmt).all()
            results: list[dict[str, Any]] = []
            for r in rows:
                results.append({
                    "price_id": r.price_id,
                    "commodity_id": r.commodity_id,
                    "commodity_name": r.commodity_name,
                    "top_category": r.top_category,
                    "sub_category": r.sub_category,
                    "group_category": r.group_category,
                    "specification": r.specification,
                    "unit": r.unit,
                    "report_date": r.report_date,
                    "price_prevailing": r.price_prevailing,
                    "is_available": r.is_available,
                    "raw_price_text": r.raw_price_text,
                    "quality": {
                        "drift_flagged": r.is_drift_flagged,
                        "drift_pct": float(r.drift_pct) if r.drift_pct is not None else None,
                    },
                })

            return latest_report_id, parser_ver, latest_date, results

    def get_price_history(
        self,
        commodity_id: int,
        from_date: date | None = None,
        to_date: date | None = None,
        region_code: str = "NCR",
    ) -> list[dict[str, Any]]:
        """Fetch historical price records for a specific commodity."""
        with self.get_session() as session:
            stmt = (
                select(
                    DailyCommodityPrice.price_id,
                    DailyCommodityPrice.report_date,
                    DailyCommodityPrice.price_prevailing,
                    DailyCommodityPrice.is_available,
                    DailyCommodityPrice.raw_price_text,
                    DailyCommodityPrice.is_drift_flagged,
                    DailyCommodityPrice.drift_pct,
                    Commodity.commodity_id,
                    Commodity.commodity_name,
                    Commodity.top_category,
                    Commodity.sub_category,
                    Commodity.group_category,
                    Commodity.specification,
                    Commodity.unit,
                )
                .join(Commodity, DailyCommodityPrice.commodity_id == Commodity.commodity_id)
                .join(PriceReport, DailyCommodityPrice.report_id == PriceReport.report_id)
                .where(
                    DailyCommodityPrice.commodity_id == commodity_id,
                    PriceReport.region_code == region_code,
                )
            )
            if from_date is not None:
                stmt = stmt.where(DailyCommodityPrice.report_date >= from_date)
            if to_date is not None:
                stmt = stmt.where(DailyCommodityPrice.report_date <= to_date)
            stmt = stmt.order_by(DailyCommodityPrice.report_date.asc())

            rows = session.execute(stmt).all()
            results: list[dict[str, Any]] = []
            for r in rows:
                results.append({
                    "price_id": r.price_id,
                    "commodity_id": r.commodity_id,
                    "commodity_name": r.commodity_name,
                    "top_category": r.top_category,
                    "sub_category": r.sub_category,
                    "group_category": r.group_category,
                    "specification": r.specification,
                    "unit": r.unit,
                    "report_date": r.report_date,
                    "price_prevailing": r.price_prevailing,
                    "is_available": r.is_available,
                    "raw_price_text": r.raw_price_text,
                    "quality": {
                        "drift_flagged": r.is_drift_flagged,
                        "drift_pct": float(r.drift_pct) if r.drift_pct is not None else None,
                    },
                })
            return results

    def get_report_by_date(self, report_date: date, region_code: str = "NCR") -> PriceReport | None:
        """Fetch report provenance metadata for a specific date and region."""
        with self.get_session() as session:
            stmt = select(PriceReport).where(
                PriceReport.region_code == region_code,
                PriceReport.report_date == report_date,
            )
            return session.scalar(stmt)

    # ------------------------------------------------------------------
    # Scheduled job bookkeeping
    # ------------------------------------------------------------------

    def record_job_run(
        self,
        job_name: str,
        started_at: datetime,
        succeeded: bool,
        detail: str | None = None,
    ) -> None:
        """Persist the outcome of one scheduled job run."""
        with self.get_session() as session, session.begin():
            session.add(JobRun(
                job_name=job_name,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                succeeded=succeeded,
                detail=detail,
            ))

    def count_consecutive_failures(self, job_name: str) -> int:
        """Number of most recent runs of `job_name` that failed in a row."""
        with self.get_session() as session:
            outcomes = session.scalars(
                select(JobRun.succeeded)
                .where(JobRun.job_name == job_name)
                .order_by(JobRun.run_id.desc())
                .limit(100)
            )
            streak = 0
            for succeeded in outcomes:
                if succeeded:
                    break
                streak += 1
            return streak
