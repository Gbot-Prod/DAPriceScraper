"""Unit tests for database repository and idempotent upsert logic."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from da_price_index.db import (
    Commodity,
    CommodityAlias,
    DailyCommodityPrice,
    DatabaseRepository,
    PriceReport,
)
from da_price_index.exporters import export_to_csv, export_to_json, prices_to_csv
from da_price_index.models import ParsedPriceRow, ParsedReport
from da_price_index.parser import parse_dpi_pdf
from da_price_index.validators import annotate_drift

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PDF_NOV_22 = FIXTURES_DIR / "Daily-Price-Index-November-22-2026.pdf"
PDF_NOV_23 = FIXTURES_DIR / "Daily-Price-Index-November-23-2026.pdf"
PDF_NOV_24 = FIXTURES_DIR / "November-24-2026-DPI-AFC.pdf"


class TestDatabaseRepository:
    """Verify database models, alias resolution, and idempotent upsert behavior."""

    def test_database_creation_and_upsert(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        repo = DatabaseRepository(db_url=f"sqlite:///{db_path}")
        try:
            repo.create_tables()

            r22 = parse_dpi_pdf(PDF_NOV_22)
            new_comm, report_id, new_prices = repo.upsert_report(r22)

            assert new_comm == 308
            assert new_prices == 308

            with repo.get_session() as session:
                # Check price_reports
                report = session.get(PriceReport, report_id)
                assert report is not None
                assert report.report_date.isoformat() == "2026-11-22"
                assert report.covered_markets_cnt == 30

                # Check commodity count
                comm_cnt = session.scalar(select(func.count(Commodity.commodity_id)))
                assert comm_cnt == 308

                # Check spot-checked price in DB: Basmati 150.00
                basmati_price = session.scalar(
                    select(DailyCommodityPrice.price_prevailing)
                    .join(Commodity)
                    .where(
                        DailyCommodityPrice.report_id == report_id,
                        Commodity.commodity_name == "Basmati",
                    )
                )
                assert basmati_price == Decimal("150.00")
        finally:
            repo.close()

    def test_cross_day_canonical_stability_in_db(self, tmp_path: Path):
        """Sequential sync across Nov 22, 23, 24 must yield 0 duplicate commodities."""
        db_path = tmp_path / "stability.db"
        repo = DatabaseRepository(db_url=f"sqlite:///{db_path}")
        try:
            repo.create_tables()

            r22 = parse_dpi_pdf(PDF_NOV_22)
            r23 = parse_dpi_pdf(PDF_NOV_23)
            r24 = parse_dpi_pdf(PDF_NOV_24)

            # Day 1: Nov 22 inserts 308 commodities
            comm1, _, prices1 = repo.upsert_report(r22)
            assert comm1 == 308
            assert prices1 == 308

            # Day 2: Nov 23 must insert 0 new commodities!
            comm2, _, prices2 = repo.upsert_report(r23)
            assert comm2 == 0, f"Expected 0 new commodities on day 2, got {comm2}"
            assert prices2 == 308

            # Day 3: Nov 24 must insert 0 new commodities!
            comm3, _, prices3 = repo.upsert_report(r24)
            assert comm3 == 0, f"Expected 0 new commodities on day 3, got {comm3}"
            assert prices3 == 308

            with repo.get_session() as session:
                total_commodities = session.scalar(select(func.count(Commodity.commodity_id)))
                total_prices = session.scalar(select(func.count(DailyCommodityPrice.price_id)))
                total_reports = session.scalar(select(func.count(PriceReport.report_id)))

                assert total_commodities == 308
                assert total_reports == 3
                assert total_prices == 308 * 3
        finally:
            repo.close()

    def test_re_run_idempotency(self, tmp_path: Path):
        """Re-running sync on the exact same reports inserts 0 new rows."""
        db_path = tmp_path / "idempotent.db"
        repo = DatabaseRepository(db_url=f"sqlite:///{db_path}")
        try:
            repo.create_tables()

            r22 = parse_dpi_pdf(PDF_NOV_22)
            r23 = parse_dpi_pdf(PDF_NOV_23)
            r24 = parse_dpi_pdf(PDF_NOV_24)

            # Initial run
            repo.upsert_report(r22)
            repo.upsert_report(r23)
            repo.upsert_report(r24)

            # Re-run all 3
            comm22_re, _, prices22_re = repo.upsert_report(r22)
            comm23_re, _, prices23_re = repo.upsert_report(r23)
            comm24_re, _, prices24_re = repo.upsert_report(r24)

            assert comm22_re == 0 and prices22_re == 0
            assert comm23_re == 0 and prices23_re == 0
            assert comm24_re == 0 and prices24_re == 0

            with repo.get_session() as session:
                total_reports = session.scalar(select(func.count(PriceReport.report_id)))
                total_commodities = session.scalar(select(func.count(Commodity.commodity_id)))
                total_prices = session.scalar(select(func.count(DailyCommodityPrice.price_id)))

                assert total_reports == 3
                assert total_commodities == 308
                assert total_prices == 308 * 3
        finally:
            repo.close()

    def test_alias_resolution(self, tmp_path: Path):
        """An item matching a CommodityAlias maps to the existing commodity, not a new one."""
        db_path = tmp_path / "alias.db"
        repo = DatabaseRepository(db_url=f"sqlite:///{db_path}")
        try:
            repo.create_tables()

            # Create existing commodity
            with repo.get_session() as session:
                comm = Commodity(
                    top_category="Rice",
                    sub_category="Imported",
                    group_category="Commercial",
                    commodity_name="Special Rice",
                    specification="Premium Grade",
                    unit="kg",
                    canonical_key="rice|imported|commercial|special rice|premium grade|kg",
                    is_active=True,
                )
                session.add(comm)
                session.flush()

                # Add alias for a modified key
                alias_key = "rice|imported|commercial|special rice (renamed)|premium grade|kg"
                alias = CommodityAlias(
                    commodity_id=comm.commodity_id,
                    canonical_key=alias_key,
                    alias_note="DA renamed commodity",
                )
                session.add(alias)
                session.commit()
                target_comm_id = comm.commodity_id

            # Ingest report containing the aliased key
            fake_report = ParsedReport(
                region_code="NCR",
                region_name="National Capital Region",
                report_date="2026-11-25",
                file_name="test.pdf",
                file_sha256="abc1234567890",
                covered_markets=["Market A"],
                items=[
                    ParsedPriceRow(
                        top_category="Rice",
                        sub_category="Imported",
                        group_category="Commercial",
                        commodity_name="Special Rice (Renamed)",
                        specification="Premium Grade",
                        unit="kg",
                        canonical_key=(
                            "rice|imported|commercial|special rice (renamed)|premium grade|kg"
                        ),
                        price_prevailing=Decimal("60.00"),
                    )
                ],
            )

            new_comm, report_id, new_prices = repo.upsert_report(fake_report)
            assert new_comm == 0, "Aliased item should not create a new commodity"
            assert new_prices == 1

            with repo.get_session() as session:
                price_record = session.scalar(
                    select(DailyCommodityPrice).where(DailyCommodityPrice.report_id == report_id)
                )
                assert price_record.commodity_id == target_comm_id
        finally:
            repo.close()

    def test_get_previous_prices_and_drift_persistence(self, tmp_path: Path):
        """Verify get_previous_prices returns previous day prices and drift flags are saved."""
        db_path = tmp_path / "drift.db"
        repo = DatabaseRepository(db_url=f"sqlite:///{db_path}")
        try:
            repo.create_tables()

            r22 = parse_dpi_pdf(PDF_NOV_22)
            repo.upsert_report(r22)

            # Check get_previous_prices before Nov 23
            prev_prices = repo.get_previous_prices("NCR", date(2026, 11, 23))
            assert len(prev_prices) > 0

            # Basmati on Nov 22 is 150.00
            basmati_key = [k for k in prev_prices if "basmati" in k][0]
            assert prev_prices[basmati_key] == Decimal("150.00")

            # Parse Nov 23 and annotate drift
            r23 = parse_dpi_pdf(PDF_NOV_23)
            annotate_drift(r23, prev_prices, threshold=0.01)  # low threshold for testing
            assert any(item.drift_pct is not None for item in r23.items)

            repo.upsert_report(r23)

            with repo.get_session() as session:
                # Verify drift_pct is persisted in database
                drift_saved = session.scalars(
                    select(DailyCommodityPrice.drift_pct)
                    .where(
                        DailyCommodityPrice.report_date == date(2026, 11, 23),
                        DailyCommodityPrice.drift_pct.is_not(None),
                    )
                ).all()
                assert len(drift_saved) > 0
        finally:
            repo.close()


class TestExporters:
    """Verify JSON and CSV export."""

    def test_json_and_csv_export(self, tmp_path: Path):
        report = parse_dpi_pdf(PDF_NOV_22)

        # JSON
        json_file = tmp_path / "export.json"
        json_str = export_to_json(report, output_path=json_file)
        assert json_file.exists()
        assert "Basmati" in json_str

        # CSV
        csv_file = tmp_path / "export.csv"
        csv_str = export_to_csv(report, output_path=csv_file)
        assert csv_file.exists()
        assert "commodity_name" in csv_str
        assert "Basmati" in csv_str

    def test_prices_to_csv(self):
        sample_rows = [
            {
                "report_date": date(2026, 11, 22),
                "commodity_id": 1,
                "commodity_name": "Basmati",
                "top_category": "Rice",
                "sub_category": "Imported",
                "group_category": "Special",
                "specification": "White",
                "unit": "kg",
                "price_prevailing": Decimal("150.00"),
                "is_available": True,
                "raw_price_text": "150.00",
                "quality": {"drift_flagged": False, "drift_pct": None},
            }
        ]
        csv_out = prices_to_csv(sample_rows)
        assert "commodity_id" in csv_out
        assert "Basmati" in csv_out
        assert "150.00" in csv_out


class TestReportDateCorrection:
    """A re-parse that corrects a report's date must move the existing row."""

    def test_reparse_corrects_misdated_report(self, tmp_path: Path):
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/fix.db")
        try:
            repo.create_tables()
            good = parse_dpi_pdf(FIXTURES_DIR / "Daily-Price-Index-November-25-2026.pdf")
            # What the old parser stored in production (report ID 4).
            bad = good.model_copy(
                update={"report_date": date(2026, 11, 13), "parser_version": "0.1.0"}
            )
            _, bad_id, _ = repo.upsert_report(bad)

            _, fixed_id, new_prices = repo.upsert_report(good)
            assert fixed_id == bad_id
            assert new_prices == len(good.items)

            assert repo.get_report_by_date(date(2026, 11, 13)) is None
            fixed = repo.get_report_by_date(date(2026, 11, 25))
            assert fixed is not None and fixed.report_id == bad_id
            assert repo.get_latest_report_info()[2] == date(2026, 11, 25)

            with repo.get_session() as session:
                dates = set(session.scalars(
                    select(DailyCommodityPrice.report_date)
                    .where(DailyCommodityPrice.report_id == bad_id)
                ).all())
            assert dates == {date(2026, 11, 25)}
        finally:
            repo.close()
