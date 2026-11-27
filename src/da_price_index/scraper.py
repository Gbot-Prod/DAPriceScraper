"""Web scraper and downloader for the Philippine DA Price Monitoring portal.

Features:
- Tenacity exponential retry backoff on network errors and transient 5xx/429 HTTP codes.
- Portal discovery with link and anchor text pattern matching for Daily Price Index (DPI).
- Content-addressable SHA-256 deduplication and JSON manifest management.
- Date-based directory structuring (data/raw/YYYY/MM/).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from da_price_index.config import settings

logger = logging.getLogger(__name__)

# Transient HTTP status codes that warrant retry
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _is_retryable_exception(exc: BaseException) -> bool:
    """Check if exception is a network failure or retryable HTTP status."""
    if isinstance(
        exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.NetworkError)
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS_CODES
    return False


@dataclass
class ScrapedLink:
    """A discovered DPI PDF link on the portal."""

    url: str
    title: str
    filename: str
    is_ncr: bool = True


@dataclass
class ManifestEntry:
    """Metadata recorded in the manifest for downloaded PDFs."""

    source_url: str
    file_name: str
    local_path: str
    file_sha256: str
    downloaded_at: str


class ManifestManager:
    """Manages the local JSON manifest of ingested/downloaded PDFs."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else settings.manifest_path
        self._entries: dict[str, ManifestEntry] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for item in data:
                    entry = ManifestEntry(**item)
                    self._entries[entry.file_sha256] = entry
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning("Failed to parse manifest %s: %s; starting fresh", self.path, e)
                self._entries = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        items = [asdict(entry) for entry in self._entries.values()]
        # Sort by downloaded_at descending
        items.sort(key=lambda x: x.get("downloaded_at", ""), reverse=True)
        self.path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def has_hash(self, sha256_hash: str) -> bool:
        return sha256_hash in self._entries

    def has_url(self, url: str) -> bool:
        return any(entry.source_url == url for entry in self._entries.values())

    def add_entry(self, entry: ManifestEntry) -> None:
        self._entries[entry.file_sha256] = entry
        self.save()

    def get_by_hash(self, sha256_hash: str) -> ManifestEntry | None:
        return self._entries.get(sha256_hash)

    def all_entries(self) -> list[ManifestEntry]:
        return list(self._entries.values())


# ---------------------------------------------------------------------------
# HTTP Client & Retrying
# ---------------------------------------------------------------------------

def create_http_client() -> httpx.Client:
    """Create a configured httpx.Client with realistic User-Agent and timeouts."""
    return httpx.Client(
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=settings.request_timeout,
        follow_redirects=True,
    )


@retry(
    retry=retry_if_exception(_is_retryable_exception),
    stop=stop_after_attempt(settings.retry_max_attempts),
    wait=wait_exponential(multiplier=2, min=settings.retry_min_wait, max=settings.retry_max_wait),
    reraise=True,
)
def fetch_portal_html(client: httpx.Client | None = None, url: str | None = None) -> str:
    """Fetch the DA Price Monitoring portal HTML with exponential backoff."""
    target_url = url or settings.portal_url
    close_client = False
    if client is None:
        client = create_http_client()
        close_client = True

    try:
        response = client.get(target_url)
        response.raise_for_status()
        return response.text
    finally:
        if close_client:
            client.close()


# ---------------------------------------------------------------------------
# Link Discovery & Filtering
# ---------------------------------------------------------------------------

_DPI_NAME_PATTERNS = [
    re.compile(r"daily[-_\s]+price[-_\s]+index", re.IGNORECASE),
    re.compile(r"\bdpi\b", re.IGNORECASE),
]

_EXCLUDE_PATTERNS = [
    re.compile(r"\bregion\s+[ivx0-9]+", re.IGNORECASE),
    re.compile(r"\bcar\b", re.IGNORECASE),
    re.compile(r"\bbarmm\b", re.IGNORECASE),
    re.compile(r"\bmimaropa\b", re.IGNORECASE),
]


def _is_dpi_link(anchor_text: str, href: str, ncr_only: bool = True) -> bool:
    """Determine if a link points to a Daily Price Index PDF."""
    href_lower = href.lower()
    if not href_lower.endswith(".pdf") and ".pdf?" not in href_lower:
        return False

    combined = f"{anchor_text} {href}"
    matches_dpi = any(p.search(combined) for p in _DPI_NAME_PATTERNS)
    if not matches_dpi:
        return False

    if ncr_only:
        # If explicitly marked for non-NCR region, exclude
        if any(p.search(combined) for p in _EXCLUDE_PATTERNS):
            return False

    return True


def discover_dpi_links(
    html: str,
    base_url: str | None = None,
    ncr_only: bool = True,
) -> list[ScrapedLink]:
    """Parse HTML and discover all Daily Price Index PDF links.

    Matches both anchor text and href attributes case-insensitively.
    """
    base = base_url or settings.portal_url
    soup = BeautifulSoup(html, "html.parser")
    found_links: list[ScrapedLink] = []
    seen_urls: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        raw_href = a_tag["href"].strip()
        anchor_text = a_tag.get_text(separator=" ").strip()
        full_url = urljoin(base, raw_href)

        if not _is_dpi_link(anchor_text, full_url, ncr_only=ncr_only):
            continue

        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # Extract filename from path
        parsed_path = urlparse(full_url).path
        filename = unquote(Path(parsed_path).name)

        found_links.append(
            ScrapedLink(
                url=full_url,
                title=anchor_text or filename,
                filename=filename,
                is_ncr=True,
            )
        )

    return found_links


# ---------------------------------------------------------------------------
# Download Logic with Content Deduplication
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception(_is_retryable_exception),
    stop=stop_after_attempt(settings.retry_max_attempts),
    wait=wait_exponential(multiplier=2, min=settings.retry_min_wait, max=settings.retry_max_wait),
    reraise=True,
)
def download_pdf(client: httpx.Client, url: str) -> bytes:
    """Download PDF binary data with exponential retry."""
    response = client.get(url)
    response.raise_for_status()
    return response.content


def download_dpi_pdf(
    url: str,
    target_dir: Path | None = None,
    client: httpx.Client | None = None,
    manifest: ManifestManager | None = None,
) -> tuple[Path, ManifestEntry, bool]:
    """Download a single DPI PDF, compute SHA-256, and record in manifest.

    Parameters
    ----------
    url : str
        The URL to the PDF.
    target_dir : Path | None
        Target directory to store downloaded PDFs. If None, uses `settings.raw_pdf_dir`.
    client : httpx.Client | None
        Optional shared httpx client.
    manifest : ManifestManager | None
        Optional manifest manager.

    Returns
    -------
    tuple[Path, ManifestEntry, bool]
        (saved_path, manifest_entry, is_new)
        `is_new` is True if newly downloaded and saved, False if already in manifest.
    """
    if manifest is None:
        manifest = ManifestManager()

    close_client = False
    if client is None:
        client = create_http_client()
        close_client = True

    try:
        content = download_pdf(client, url)
        sha256_hash = hashlib.sha256(content).hexdigest()

        # Check manifest
        if manifest.has_hash(sha256_hash):
            existing = manifest.get_by_hash(sha256_hash)
            assert existing is not None
            logger.info(
                "PDF %s already downloaded (hash: %s...)", existing.file_name, sha256_hash[:8]
            )
            return Path(existing.local_path), existing, False

        # Determine filename and directory (data/raw/YYYY/MM/)
        parsed = urlparse(url)
        orig_filename = unquote(Path(parsed.path).name) or f"dpi_{sha256_hash[:8]}.pdf"

        now = datetime.now(timezone.utc)
        base_dir = target_dir or settings.raw_pdf_dir
        dest_dir = base_dir / f"{now.year:04d}" / f"{now.month:02d}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest_file = dest_dir / orig_filename
        # Avoid filename collision if different file had same name
        if dest_file.exists():
            existing_hash = hashlib.sha256(dest_file.read_bytes()).hexdigest()
            if existing_hash != sha256_hash:
                dest_file = dest_dir / f"{dest_file.stem}_{sha256_hash[:8]}{dest_file.suffix}"

        dest_file.write_bytes(content)

        entry = ManifestEntry(
            source_url=url,
            file_name=dest_file.name,
            local_path=str(dest_file),
            file_sha256=sha256_hash,
            downloaded_at=now.isoformat(),
        )
        manifest.add_entry(entry)
        logger.info("Downloaded %s -> %s (hash: %s...)", orig_filename, dest_file, sha256_hash[:8])
        return dest_file, entry, True

    finally:
        if close_client:
            client.close()


@dataclass
class PortalSyncResult:
    """Detailed outcome of a portal discovery + download pass."""

    links_found: int
    entries: list[ManifestEntry]
    errors: list[str]


def sync_portal_pdfs(
    limit: int | None = 5,
    portal_url: str | None = None,
    target_dir: Path | None = None,
    manifest: ManifestManager | None = None,
    client: httpx.Client | None = None,
) -> list[ManifestEntry]:
    """Discover and download recent DPI PDFs; returns the downloaded entries.

    Per-file download failures are logged and skipped. Use
    `sync_portal_pdfs_detailed` when the caller needs to know about them.
    """
    return sync_portal_pdfs_detailed(
        limit=limit,
        portal_url=portal_url,
        target_dir=target_dir,
        manifest=manifest,
        client=client,
    ).entries


def sync_portal_pdfs_detailed(
    limit: int | None = 5,
    portal_url: str | None = None,
    target_dir: Path | None = None,
    manifest: ManifestManager | None = None,
    client: httpx.Client | None = None,
) -> PortalSyncResult:
    """Discover and download recent Daily Price Index PDFs from the DA portal.

    Portal-page fetch errors (after retries) propagate. Per-file download errors
    are collected in `errors`; `links_found` is the number of DPI links on the
    page before `limit` is applied (0 usually means the portal layout changed).

    Parameters
    ----------
    limit : int | None
        Maximum number of PDFs to download (default 5). None for all.
    portal_url : str | None
        Override portal URL.
    target_dir : Path | None
        Target directory for downloads.
    manifest : ManifestManager | None
        Manifest manager instance.
    client : httpx.Client | None
        Optional shared HTTP client.

    Returns
    -------
    PortalSyncResult
        Entries for newly ingested or already present PDFs, plus diagnostics.
    """
    if manifest is None:
        manifest = ManifestManager()

    close_client = False
    if client is None:
        client = create_http_client()
        close_client = True

    results: list[ManifestEntry] = []
    errors: list[str] = []
    try:
        html = fetch_portal_html(client, url=portal_url)
        links = discover_dpi_links(html, base_url=portal_url or settings.portal_url)
        links_found = len(links)

        if limit is not None:
            links = links[:limit]

        for link in links:
            try:
                _, entry, _ = download_dpi_pdf(
                    link.url,
                    target_dir=target_dir,
                    client=client,
                    manifest=manifest,
                )
                results.append(entry)
            except Exception as e:
                logger.error("Failed to download PDF from %s: %s", link.url, e)
                errors.append(f"Download failed for {link.url}: {e}")

        return PortalSyncResult(links_found=links_found, entries=results, errors=errors)
    finally:
        if close_client:
            client.close()
