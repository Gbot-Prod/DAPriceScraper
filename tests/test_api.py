"""Integration tests for the FastAPI HTTP reference service."""

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from da_price_index.api import create_app
from da_price_index.db import DatabaseRepository
from da_price_index.parser import parse_dpi_pdf

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PDF_NOV_22 = FIXTURES_DIR / "Daily-Price-Index-November-22-2026.pdf"
PDF_NOV_23 = FIXTURES_DIR / "Daily-Price-Index-November-23-2026.pdf"
PDF_NOV_24 = FIXTURES_DIR / "November-24-2026-DPI-AFC.pdf"


class TestAPI:
    """Test HTTP API endpoints, formats, headers, and caching."""

    def test_health_endpoint(self, tmp_path: Path):
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/health.db")
        try:
            repo.create_tables()
            client = TestClient(create_app(repo))

            response = client.get("/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["service"] == "DAPriceScraper"
            assert data["latest_report"] is None

            # After ingesting report, latest_report is populated
            report = parse_dpi_pdf(PDF_NOV_22)
            repo.upsert_report(report)

            response2 = client.get("/health")
            assert response2.status_code == 200
            data2 = response2.json()
            assert data2["latest_report"] is not None
            assert data2["latest_report"]["report_date"] == "2026-11-22"
        finally:
            repo.close()

    def test_commodities_list_and_filters(self, tmp_path: Path):
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/comm.db")
        try:
            repo.create_tables()
            report = parse_dpi_pdf(PDF_NOV_22)
            repo.upsert_report(report)

            client = TestClient(create_app(repo))

            # List all
            res = client.get("/commodities")
            assert res.status_code == 200
            items = res.json()
            assert len(items) == 308
            first = items[0]
            assert "commodity_id" in first
            assert "commodity_name" in first
            assert "canonical_key" in first

            # Search filter
            res_search = client.get("/commodities?search=basmati")
            assert res_search.status_code == 200
            search_items = res_search.json()
            assert len(search_items) >= 1
            assert any("Basmati" in it["commodity_name"] for it in search_items)

            # Category filter
            res_cat = client.get("/commodities?category=Meat")
            assert res_cat.status_code == 200
            assert all("Meat" in it["top_category"] for it in res_cat.json())
        finally:
            repo.close()

    def test_prices_latest_json_and_etag_caching(self, tmp_path: Path):
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/prices.db")
        try:
            repo.create_tables()
            report = parse_dpi_pdf(PDF_NOV_22)
            repo.upsert_report(report)

            client = TestClient(create_app(repo))

            # 1. Fetch latest prices JSON
            res = client.get("/prices/latest?region=NCR")
            assert res.status_code == 200
            assert "ETag" in res.headers
            assert "Cache-Control" in res.headers
            etag = res.headers["ETag"]

            data = res.json()
            assert data["region_code"] == "NCR"
            assert data["as_of_date"] == "2026-11-22"
            assert data["total_commodities"] == 308

            item = data["items"][0]
            assert "commodity_id" in item
            assert "price_prevailing" in item
            assert "report_date" in item
            assert "quality" in item
            assert "drift_flagged" in item["quality"]

            # 2. Test 304 Not Modified
            res_cached = client.get("/prices/latest?region=NCR", headers={"If-None-Match": etag})
            assert res_cached.status_code == 304

            # 3. Filter by single commodity_id
            target_id = item["commodity_id"]
            res_single = client.get(f"/prices/latest?region=NCR&commodity_id={target_id}")
            assert res_single.status_code == 200
            single_data = res_single.json()
            assert single_data["total_commodities"] == 1
            assert single_data["items"][0]["commodity_id"] == target_id
        finally:
            repo.close()

    def test_prices_latest_csv_export(self, tmp_path: Path):
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/csv.db")
        try:
            repo.create_tables()
            report = parse_dpi_pdf(PDF_NOV_22)
            repo.upsert_report(report)

            client = TestClient(create_app(repo))

            # 1. Via ?format=csv
            res = client.get("/prices/latest?format=csv")
            assert res.status_code == 200
            assert "text/csv" in res.headers["content-type"]
            assert "report_date,commodity_id,commodity_name" in res.text
            assert "Basmati" in res.text

            # 2. Via Accept: text/csv header
            res_accept = client.get("/prices/latest", headers={"Accept": "text/csv"})
            assert res_accept.status_code == 200
            assert "text/csv" in res_accept.headers["content-type"]
            assert "Basmati" in res_accept.text
        finally:
            repo.close()

    def test_prices_history_time_series(self, tmp_path: Path):
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/history.db")
        try:
            repo.create_tables()
            repo.upsert_report(parse_dpi_pdf(PDF_NOV_22))
            repo.upsert_report(parse_dpi_pdf(PDF_NOV_23))

            client = TestClient(create_app(repo))

            # Get basmati ID
            comm_res = client.get("/commodities?search=basmati")
            basmati_id = comm_res.json()[0]["commodity_id"]

            # Fetch history across Nov 22 and 23
            res = client.get(f"/prices?commodity_id={basmati_id}&from=2026-11-22&to=2026-11-23")
            assert res.status_code == 200
            history = res.json()
            assert len(history) == 2
            assert history[0]["report_date"] == "2026-11-22"
            assert history[1]["report_date"] == "2026-11-23"

            # CSV format for time-series
            res_csv = client.get(f"/prices?commodity_id={basmati_id}&format=csv")
            assert res_csv.status_code == 200
            assert "text/csv" in res_csv.headers["content-type"]
            assert "2026-11-22" in res_csv.text

            # Known commodity, empty date window -> 200 with empty list
            res_empty = client.get(
                f"/prices?commodity_id={basmati_id}&from=1999-01-01&to=1999-01-02"
            )
            assert res_empty.status_code == 200
            assert res_empty.json() == []

            # Unknown commodity -> 404
            res_404 = client.get("/prices?commodity_id=999999")
            assert res_404.status_code == 404
            assert "not found" in res_404.json()["detail"]
        finally:
            repo.close()

    def test_reports_provenance(self, tmp_path: Path):
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/prov.db")
        try:
            repo.create_tables()
            repo.upsert_report(parse_dpi_pdf(PDF_NOV_22))

            client = TestClient(create_app(repo))

            # Found
            res = client.get("/reports/2026-11-22")
            assert res.status_code == 200
            data = res.json()
            assert data["report_date"] == "2026-11-22"
            assert data["region_code"] == "NCR"
            assert len(data["covered_markets"]) == 30
            assert data["covered_markets_cnt"] == 30
            assert "file_sha256" in data

            # Not found
            res_404 = client.get("/reports/1999-01-01")
            assert res_404.status_code == 404
        finally:
            repo.close()

    def test_not_found_on_empty_db(self, tmp_path: Path):
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/empty.db")
        try:
            repo.create_tables()
            client = TestClient(create_app(repo))

            res = client.get("/prices/latest?region=NCR")
            assert res.status_code == 404
            assert "No price reports found" in res.json()["detail"]
        finally:
            repo.close()


class TestPriceSummary:
    """GET /prices/summary trailing-window bands."""

    def test_summary_bands(self, tmp_path: Path):
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/summary.db")
        try:
            repo.create_tables()
            reports = [parse_dpi_pdf(p) for p in (PDF_NOV_22, PDF_NOV_23, PDF_NOV_24)]
            for r in reports:
                repo.upsert_report(r)
            client = TestClient(create_app(repo))

            res = client.get("/prices/summary?days=30")
            assert res.status_code == 200
            data = res.json()
            assert data["window_end"] == "2026-11-24"
            assert data["window_start"] == "2026-10-26"
            assert data["total_commodities"] > 0
            assert "ETag" in res.headers

            basmati = next(i for i in data["items"] if i["commodity_name"] == "Basmati")
            expected = sorted(
                next(it.price_prevailing for it in r.items if it.commodity_name == "Basmati")
                for r in reports
            )
            assert basmati["observations"] == 3
            assert Decimal(basmati["price_min"]) == expected[0]
            assert Decimal(basmati["price_max"]) == expected[-1]
            assert Decimal(basmati["price_median"]) == expected[1]
            assert basmati["first_date"] == "2026-11-22"
            assert basmati["last_date"] == "2026-11-24"

            # days=1 -> only the latest report
            one = client.get(
                f"/prices/summary?days=1&commodity_id={basmati['commodity_id']}"
            ).json()
            assert one["total_commodities"] == 1
            assert one["items"][0]["observations"] == 1

            # ETag round-trip
            res304 = client.get(
                "/prices/summary?days=30", headers={"If-None-Match": res.headers["ETag"]}
            )
            assert res304.status_code == 304

            assert client.get("/prices/summary?days=0").status_code == 422
        finally:
            repo.close()

    def test_summary_404_on_empty_db(self, tmp_path: Path):
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/empty_summary.db")
        try:
            repo.create_tables()
            res = TestClient(create_app(repo)).get("/prices/summary")
            assert res.status_code == 404
        finally:
            repo.close()
