"""Command-line interface (CLI) for DAPriceScraper (da-dpi)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from da_price_index.alerts import send_alert
from da_price_index.config import settings
from da_price_index.db import DatabaseRepository
from da_price_index.exporters import export_to_csv, export_to_json
from da_price_index.parser import parse_dpi_pdf
from da_price_index.scraper import ManifestManager, sync_portal_pdfs, sync_portal_pdfs_detailed
from da_price_index.validators import ValidationError, annotate_drift, validate_report

app = typer.Typer(
    name="da-dpi",
    help=(
        "Philippine Department of Agriculture Daily Price Index scraper, parser, "
        "and benchmark sync tool."
    ),
    add_completion=False,
)
console = Console()


@app.command()
def fetch(
    limit: int = typer.Option(
        5, "--limit", "-l", help="Maximum number of PDFs to discover and download"
    ),
    portal_url: str | None = typer.Option(
        None, "--url", "-u", help="Override DA price monitoring portal URL"
    ),
    output_dir: Path | None = typer.Option(
        None, "--output-dir", "-o", help="Directory to save downloaded PDFs"
    ),
) -> None:
    """Discover and download recent Daily Price Index PDFs from the DA portal."""
    rprint(
        f"[bold cyan]Fetching up to {limit} Daily Price Index PDFs from DA portal...[/bold cyan]"
    )
    manifest = ManifestManager()

    try:
        entries = sync_portal_pdfs(
            limit=limit,
            portal_url=portal_url,
            target_dir=output_dir,
            manifest=manifest,
        )
        if not entries:
            rprint("[yellow]No new or matching DPI PDFs found on the portal.[/yellow]")
            return

        table = Table(title="Downloaded / Synced PDFs")
        table.add_column("Filename", style="green")
        table.add_column("SHA-256 (Prefix)", style="dim")
        table.add_column("Local Path")

        for entry in entries:
            table.add_row(entry.file_name, entry.file_sha256[:12] + "...", entry.local_path)

        console.print(table)
        rprint(f"[bold green]Successfully processed {len(entries)} file(s).[/bold green]")

    except Exception as e:
        rprint(f"[bold red]Fetch failed:[/bold red] {e}")
        raise typer.Exit(code=1) from e


@app.command()
def parse(
    pdf_path: Path = typer.Argument(
        ..., help="Path to local DPI PDF file", exists=True, dir_okay=False
    ),
    format: str = typer.Option("table", "--format", "-f", help="Output format: table, json, csv"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Optional output file path"),
) -> None:
    """Parse a single local DPI PDF and output structured commodity data."""
    try:
        report = parse_dpi_pdf(pdf_path)
        validate_report(report)

        if format.lower() == "json":
            result = export_to_json(report, output_path=output)
            if output is None:
                print(result)
            else:
                rprint(f"[green]Saved JSON export to {output}[/green]")
        elif format.lower() == "csv":
            result = export_to_csv(report, output_path=output)
            if output is None:
                print(result)
            else:
                rprint(f"[green]Saved CSV export to {output}[/green]")
        else:
            # Rich table summary
            table = Table(title=f"DPI Report: {report.report_date} ({report.region_code})")
            table.add_column("Category", style="cyan")
            table.add_column("Commodity", style="white")
            table.add_column("Specification", style="dim")
            table.add_column("Price (PHP)", justify="right", style="green")
            table.add_column("Unit", justify="center")

            # Show first 25 items in table
            for item in report.items[:25]:
                price_str = (
                    f"{item.price_prevailing:.2f}" if item.price_prevailing is not None else "n/a"
                )
                cat = item.sub_category or item.top_category
                table.add_row(cat, item.commodity_name, item.specification, price_str, item.unit)

            console.print(table)
            rprint(f"[dim]Showing 25 of {len(report.items)} total commodities.[/dim]")
            rprint(
                f"[bold]Covered Markets ({len(report.covered_markets)}):[/bold] "
                f"{', '.join(report.covered_markets[:5])}..."
            )

    except ValidationError as e:
        rprint(f"[bold red]Validation failed for {pdf_path}:[/bold red]")
        for err in e.errors:
            rprint(f"  • {err}")
        raise typer.Exit(code=1) from e
    except Exception as e:
        rprint(f"[bold red]Parse error:[/bold red] {e}")
        raise typer.Exit(code=1) from e


@dataclass
class SyncOutcome:
    """Aggregate result of one sync pass. Any entry in `errors` means failure."""

    new_commodities: int = 0
    new_prices: int = 0
    files_processed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _run_sync(repo: DatabaseRepository, pdf_path: Path | None, limit: int) -> SyncOutcome:
    """Fetch (or take a local file), parse, validate and upsert, collecting errors."""
    outcome = SyncOutcome()
    pdfs_to_process: list[tuple[Path, str | None]] = []

    if pdf_path is not None:
        pdfs_to_process.append((pdf_path, None))
    else:
        rprint("[bold cyan]Fetching recent DPI PDFs from portal...[/bold cyan]")
        try:
            portal = sync_portal_pdfs_detailed(limit=limit, manifest=ManifestManager())
        except Exception as e:
            outcome.errors.append(f"Portal fetch failed: {e}")
            return outcome
        if portal.links_found == 0:
            outcome.errors.append(
                f"No Daily Price Index links found on {settings.portal_url} "
                "- the portal layout may have changed."
            )
            return outcome
        outcome.errors.extend(portal.errors)
        for entry in portal.entries:
            pdfs_to_process.append((Path(entry.local_path), entry.source_url))

    if not pdfs_to_process:
        rprint("[yellow]No PDFs to sync.[/yellow]")
        return outcome

    # Parse everything first, then upsert oldest -> newest. The portal lists
    # newest first; upserting in that order would compute each day's drift
    # before the previous day exists in the DB (initial backfill, or catching
    # up on several days after an outage).
    parsed = []
    for path, source_url in pdfs_to_process:
        try:
            parsed.append((parse_dpi_pdf(path), path, source_url))
        except Exception as e:
            rprint(f"  [bold red]Error parsing {path.name}:[/bold red] {e}")
            outcome.errors.append(f"{path.name}: parse failed: {type(e).__name__}: {e}")
    parsed.sort(key=lambda item: item[0].report_date)

    for report, path, source_url in parsed:
        rprint(f"\nProcessing [bold]{path.name}[/bold] ({report.report_date})...")
        for w in report.date_warnings:
            rprint(f"  [yellow]Date warning:[/yellow] {w}")
        try:
            prev_prices = repo.get_previous_prices(report.region_code, report.report_date)
            drift_warnings = annotate_drift(report, prev_prices)
            for w in drift_warnings:
                rprint(f"  [yellow]Drift warning:[/yellow] {w.message}")

            validate_report(report)
            new_comm, report_id, new_prices = repo.upsert_report(report, source_url=source_url)

            if new_prices > 0:
                rprint(
                    f"  [green]✓ Synced report ID {report_id}: {new_comm} new commodities, "
                    f"{new_prices} prices.[/green]"
                )
            else:
                rprint(f"  [dim]✓ Report ID {report_id} already up to date (0 new rows).[/dim]")

            outcome.new_commodities += new_comm
            outcome.new_prices += new_prices
            outcome.files_processed += 1

        except ValidationError as e:
            rprint(f"  [bold red]Validation error:[/bold red] {e}")
            outcome.errors.append(f"{path.name}: validation failed: {e}")
        except Exception as e:
            rprint(f"  [bold red]Error syncing {path.name}:[/bold red] {e}")
            outcome.errors.append(f"{path.name}: {type(e).__name__}: {e}")

    return outcome


def _print_summary(outcome: SyncOutcome) -> None:
    rprint(
        f"\n[bold green]Sync finished:[/bold green] {outcome.new_commodities} new commodity "
        f"definitions, {outcome.new_prices} price rows upserted."
    )
    if outcome.errors:
        rprint(f"[bold red]{len(outcome.errors)} error(s):[/bold red]")
        for err in outcome.errors:
            rprint(f"  • {err}")


@app.command()
def sync(
    pdf_path: Path | None = typer.Option(
        None, "--file", "-f", help="Specific PDF file to parse and sync"
    ),
    db_url: str | None = typer.Option(
        None, "--db-url", help="Database connection URL (Postgres or SQLite)"
    ),
    limit: int = typer.Option(
        5, "--limit", "-l", help="Number of recent PDFs to fetch if no file specified"
    ),
) -> None:
    """End-to-end sync: fetch, parse, validate, and upsert DPI reports into relational database.

    Exits with code 1 if any step failed.
    """
    if pdf_path is not None and not pdf_path.exists():
        rprint(f"[bold red]File not found:[/bold red] {pdf_path}")
        raise typer.Exit(code=1)

    repo = DatabaseRepository(db_url=db_url or settings.db_url)
    try:
        repo.create_tables()
        outcome = _run_sync(repo, pdf_path, limit)
    finally:
        repo.close()

    _print_summary(outcome)
    if not outcome.ok:
        raise typer.Exit(code=1)


@app.command()
def serve(
    host: str = typer.Option(settings.api_host, "--host", "-h", help="Bind host address"),
    port: int = typer.Option(settings.api_port, "--port", "-p", help="Bind port number"),
    reload: bool = typer.Option(False, "--reload", "-r", help="Enable auto-reload for development"),
) -> None:
    """Start the DA Price Index HTTP reference API service."""
    import uvicorn
    rprint(f"[bold cyan]Starting DA Price Index API on http://{host}:{port}...[/bold cyan]")
    uvicorn.run("da_price_index.api:app", host=host, port=port, reload=reload)


DAILY_JOB_NAME = "run-daily"


def _alert_on_streak(previous_streak: int, outcome: SyncOutcome) -> None:
    """Alert once when failures reach the threshold, and once on recovery."""
    threshold = max(1, settings.alert_after_failures)
    if not outcome.ok:
        streak = previous_streak + 1
        if streak == threshold:
            details = "\n".join(f"- {e}" for e in outcome.errors)
            send_alert(
                f":rotating_light: DA Price Index daily sync failed {streak} times in a row.\n"
                f"Latest errors:\n{details}"
            )
    elif previous_streak >= threshold:
        send_alert(
            f":white_check_mark: DA Price Index daily sync recovered after "
            f"{previous_streak} consecutive failures."
        )


@app.command(name="run-daily")
def run_daily(
    db_url: str | None = typer.Option(None, "--db-url", help="Database connection URL"),
) -> None:
    """Automated daily workflow: fetch newly published PDFs, parse, validate, and upsert.

    Records each run; alerts the configured webhook after consecutive failures.
    Exits with code 1 on failure.
    """
    rprint("[bold blue]Starting daily DA Price Index sync job...[/bold blue]")
    started_at = datetime.now(UTC)

    def _database_unavailable(e: Exception) -> typer.Exit:
        # Without the database the failure streak can't be tracked, and the
        # API can't be serving fresh data either -- alert right away.
        rprint(f"[bold red]Database unavailable:[/bold red] {e}")
        send_alert(
            f":rotating_light: DA Price Index daily sync could not reach the database: {e}"
        )
        return typer.Exit(code=1)

    try:
        # Engine creation imports the DB driver, so a missing driver lands here too.
        repo = DatabaseRepository(db_url=db_url or settings.db_url)
    except Exception as e:
        raise _database_unavailable(e) from e

    try:
        try:
            repo.create_tables()
            previous_streak = repo.count_consecutive_failures(DAILY_JOB_NAME)
        except Exception as e:
            raise _database_unavailable(e) from e

        try:
            outcome = _run_sync(repo, None, limit=3)
        except Exception as e:
            outcome = SyncOutcome(errors=[f"Unexpected error: {type(e).__name__}: {e}"])
        _print_summary(outcome)

        try:
            repo.record_job_run(
                DAILY_JOB_NAME,
                started_at,
                succeeded=outcome.ok,
                detail="\n".join(outcome.errors) or None,
            )
        except Exception as e:
            rprint(f"[bold red]Could not record job run:[/bold red] {e}")
            send_alert(
                f":rotating_light: DA Price Index daily sync could not record its result: {e}"
            )
            raise typer.Exit(code=1) from e

        _alert_on_streak(previous_streak, outcome)
    finally:
        repo.close()

    if not outcome.ok:
        raise typer.Exit(code=1)
    rprint("[bold blue]Daily job finished.[/bold blue]")


if __name__ == "__main__":
    app()
