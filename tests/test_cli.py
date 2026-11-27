"""Unit tests for the Typer CLI commands."""

from pathlib import Path

from typer.testing import CliRunner

from da_price_index.cli import app

runner = CliRunner()
FIXTURES_DIR = Path(__file__).parent / "fixtures"
PDF_NOV_22 = FIXTURES_DIR / "Daily-Price-Index-November-22-2026.pdf"


class TestCLI:
    """Verify CLI commands."""

    def test_cli_help(self, tmp_path: Path | None = None):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "da-dpi" in result.output
        assert "serve" in result.output

    def test_cli_serve_help(self, tmp_path: Path | None = None):
        result = runner.invoke(app, ["serve", "--help"])
        assert result.exit_code == 0
        assert "Start the DA Price Index HTTP reference API service" in result.output

    def test_cli_parse_table(self, tmp_path: Path | None = None):
        result = runner.invoke(app, ["parse", str(PDF_NOV_22), "--format", "table"])
        assert result.exit_code == 0
        assert "Basmati" in result.output

    def test_cli_parse_json(self, tmp_path: Path):
        out_json = tmp_path / "cli_out.json"
        result = runner.invoke(
            app, ["parse", str(PDF_NOV_22), "--format", "json", "--output", str(out_json)]
        )
        assert result.exit_code == 0
        assert out_json.exists()
        assert "Basmati" in out_json.read_text(encoding="utf-8")

    def test_cli_sync_single_file(self, tmp_path: Path):
        db_file = tmp_path / "cli_sync.db"
        result = runner.invoke(app, ["sync", "--file", str(PDF_NOV_22), "--db-url", f"sqlite:///{db_file}"])
        assert result.exit_code == 0
        assert "308 new commodity definitions" in result.output
        assert "308 price rows upserted" in result.output

        # Re-run -> 0 new rows
        result2 = runner.invoke(app, ["sync", "--file", str(PDF_NOV_22), "--db-url", f"sqlite:///{db_file}"])
        assert result2.exit_code == 0
        assert "0 new commodity definitions" in result2.output
        assert "0 price rows upserted" in result2.output
