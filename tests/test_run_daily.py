"""Tests for run-daily failure detection, job-run tracking, and alerting."""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from da_price_index.cli import app
from da_price_index.db import DatabaseRepository, normalize_db_url
from da_price_index.scraper import ManifestEntry, PortalSyncResult

runner = CliRunner()
FIXTURES_DIR = Path(__file__).parent / "fixtures"
PDF_NOV_22 = FIXTURES_DIR / "Daily-Price-Index-November-22-2026.pdf"


def _ok_portal() -> PortalSyncResult:
    entry = ManifestEntry(
        source_url="https://example.test/dpi.pdf",
        file_name=PDF_NOV_22.name,
        local_path=str(PDF_NOV_22),
        file_sha256="x" * 64,
        downloaded_at="2026-11-22T00:00:00+00:00",
    )
    return PortalSyncResult(links_found=1, entries=[entry], errors=[])


def _run(db_url: str, portal):
    """Invoke run-daily with the portal call replaced; returns (result, alerts)."""
    alerts: list[str] = []
    kwargs = {"side_effect": portal} if callable(portal) or isinstance(portal, Exception) else {
        "return_value": portal
    }
    with patch("da_price_index.cli.sync_portal_pdfs_detailed", **kwargs), \
         patch("da_price_index.cli.send_alert", side_effect=lambda m: alerts.append(m)):
        result = runner.invoke(app, ["run-daily", "--db-url", db_url])
    return result, alerts


class TestRunDaily:
    """run-daily must fail loudly and alert after 2 consecutive failures."""

    def test_success_exits_zero_and_records_run(self, tmp_path: Path):
        db_url = f"sqlite:///{tmp_path}/ok.db"
        result, alerts = _run(db_url, _ok_portal())
        assert result.exit_code == 0, result.output
        assert alerts == []
        repo = DatabaseRepository(db_url=db_url)
        try:
            assert repo.count_consecutive_failures("run-daily") == 0
        finally:
            repo.close()

    def test_alerts_only_on_second_consecutive_failure_then_recovery(self, tmp_path: Path):
        db_url = f"sqlite:///{tmp_path}/streak.db"
        down = ConnectionError("portal unreachable")

        r1, a1 = _run(db_url, down)
        assert r1.exit_code == 1
        assert a1 == []  # first failure: no alert yet

        r2, a2 = _run(db_url, down)
        assert r2.exit_code == 1
        assert len(a2) == 1
        assert "failed 2 times in a row" in a2[0]
        assert "portal unreachable" in a2[0]

        r3, a3 = _run(db_url, down)
        assert r3.exit_code == 1
        assert a3 == []  # no repeat spam while still down

        r4, a4 = _run(db_url, _ok_portal())
        assert r4.exit_code == 0, r4.output
        assert len(a4) == 1
        assert "recovered after 3 consecutive failures" in a4[0]

    def test_success_between_failures_resets_streak(self, tmp_path: Path):
        db_url = f"sqlite:///{tmp_path}/reset.db"
        down = ConnectionError("boom")
        _run(db_url, down)
        _, alerts_ok = _run(db_url, _ok_portal())
        assert alerts_ok == []  # single failure never alerted, so no recovery notice
        _, alerts_fail = _run(db_url, down)
        assert alerts_fail == []

    def test_zero_links_on_portal_is_a_failure(self, tmp_path: Path):
        db_url = f"sqlite:///{tmp_path}/layout.db"
        empty = PortalSyncResult(links_found=0, entries=[], errors=[])
        result, _ = _run(db_url, empty)
        assert result.exit_code == 1
        assert "portal layout may have changed" in result.output

    def test_download_errors_are_a_failure(self, tmp_path: Path):
        db_url = f"sqlite:///{tmp_path}/dl.db"
        partial = _ok_portal()
        partial.errors.append("Download failed for https://example.test/b.pdf: 404")
        result, _ = _run(db_url, partial)
        assert result.exit_code == 1
        assert "Download failed" in result.output

    def test_unreachable_database_alerts_immediately(self, tmp_path: Path):
        alerts: list[str] = []
        with patch("da_price_index.cli.send_alert", side_effect=lambda m: alerts.append(m)):
            result = runner.invoke(
                app, ["run-daily", "--db-url", "postgresql://u:p@127.0.0.1:1/nope"]
            )
        assert result.exit_code == 1
        assert len(alerts) == 1
        assert "could not reach the database" in alerts[0]

    def test_sync_exits_nonzero_on_parse_failure(self, tmp_path: Path):
        bad_pdf = tmp_path / "not-a-pdf.pdf"
        bad_pdf.write_bytes(b"garbage")
        result = runner.invoke(
            app, ["sync", "--file", str(bad_pdf), "--db-url", f"sqlite:///{tmp_path}/bad.db"]
        )
        assert result.exit_code == 1
        assert "1 error(s)" in result.output
        assert "parse failed" in result.output


class TestDbUrlNormalization:
    def test_postgres_urls_use_psycopg3(self, tmp_path: Path | None = None):
        assert normalize_db_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
        assert normalize_db_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"

    def test_other_urls_untouched(self, tmp_path: Path | None = None):
        for url in ("sqlite:///data/x.db", "postgresql+psycopg://h/db", "postgresql+psycopg2://h/db"):
            assert normalize_db_url(url) == url
