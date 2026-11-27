"""Unit tests for DA DPI portal scraper and downloader."""

import hashlib
from pathlib import Path

import httpx

try:
    import pytest
except ImportError:
    pytest = None

from da_price_index.scraper import (
    ManifestEntry,
    ManifestManager,
    discover_dpi_links,
    download_dpi_pdf,
    sync_portal_pdfs,
)

SAMPLE_PORTAL_HTML = """
<!DOCTYPE html>
<html>
<head><title>DA Price Monitoring</title></head>
<body>
  <h1>Price Monitoring</h1>
  <div class="content">
    <!-- Valid NCR DPI links with varied anchor text and filenames -->
    <a href="https://www.da.gov.ph/wp-content/uploads/2026/11/Daily-Price-Index-November-22-2026.pdf">
      Daily Price Index - November 22, 2026
    </a>
    <a href="/wp-content/uploads/2026/11/Daily-Price-Index-November-23-2026.pdf">
      Daily Price Index for November 23, 2026
    </a>
    <a href="November-24-2026-DPI-AFC.pdf">
      DPI Report
    </a>

    <!-- Regional reports that should be excluded when ncr_only=True -->
    <a href="https://www.da.gov.ph/wp-content/uploads/2026/11/Region-IV-A-Daily-Price-Index-Nov-22.pdf">
      Region IV-A Daily Price Index
    </a>
    <a href="https://www.da.gov.ph/wp-content/uploads/2026/11/CAR-DPI-Nov-22.pdf">
      CAR DPI
    </a>

    <!-- Non-PDF links -->
    <a href="https://www.da.gov.ph/about/">About Us</a>
    <a href="https://www.da.gov.ph/price-monitoring/guidelines.docx">Guidelines</a>
  </div>
</body>
</html>
"""


class TestScraperDiscovery:
    """Test link discovery and filtering."""

    def test_discover_dpi_links_ncr(self):
        base_url = "https://www.da.gov.ph/price-monitoring/"
        links = discover_dpi_links(SAMPLE_PORTAL_HTML, base_url=base_url, ncr_only=True)

        # Should find exactly the 3 NCR files
        assert len(links) == 3

        filenames = [link.filename for link in links]
        assert "Daily-Price-Index-November-22-2026.pdf" in filenames
        assert "Daily-Price-Index-November-23-2026.pdf" in filenames
        assert "November-24-2026-DPI-AFC.pdf" in filenames

        # Verify relative URL was resolved correctly
        assert links[1].url == "https://www.da.gov.ph/wp-content/uploads/2026/11/Daily-Price-Index-November-23-2026.pdf"
        assert links[2].url == "https://www.da.gov.ph/price-monitoring/November-24-2026-DPI-AFC.pdf"

    def test_discover_dpi_links_include_all(self):
        links = discover_dpi_links(SAMPLE_PORTAL_HTML, ncr_only=False)
        # Should include NCR + regional reports (5 total)
        assert len(links) == 5


class TestManifestManager:
    """Test manifest persistence and deduplication."""

    def test_manifest_lifecycle(self, tmp_path: Path):
        manifest_file = tmp_path / "manifest.json"
        mgr = ManifestManager(path=manifest_file)

        assert len(mgr.all_entries()) == 0
        assert not mgr.has_hash("dummy_hash")

        entry = ManifestEntry(
            source_url="https://example.com/test.pdf",
            file_name="test.pdf",
            local_path=str(tmp_path / "test.pdf"),
            file_sha256="abc12345" * 8,
            downloaded_at="2026-11-24T12:00:00Z",
        )
        mgr.add_entry(entry)

        assert mgr.has_hash("abc12345" * 8)
        assert mgr.has_url("https://example.com/test.pdf")

        # Reload from disk
        mgr2 = ManifestManager(path=manifest_file)
        assert mgr2.has_hash("abc12345" * 8)
        retrieved = mgr2.get_by_hash("abc12345" * 8)
        assert retrieved is not None
        assert retrieved.file_name == "test.pdf"


class TestDownloader:
    """Test idempotent PDF downloading."""

    def test_download_dpi_pdf_idempotency(self, tmp_path: Path):
        fake_pdf_content = b"%PDF-1.4 Fake PDF Content for Unit Test"
        expected_hash = hashlib.sha256(fake_pdf_content).hexdigest()

        # Mock transport
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=fake_pdf_content)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        manifest_path = tmp_path / "manifest.json"
        manifest = ManifestManager(path=manifest_path)
        download_dir = tmp_path / "downloads"

        # 1. First download -> is_new == True
        url = "https://www.da.gov.ph/Daily-Price-Index-November-22-2026.pdf"
        saved_path, entry, is_new = download_dpi_pdf(
            url,
            target_dir=download_dir,
            client=client,
            manifest=manifest,
        )

        assert is_new is True
        assert saved_path.exists()
        assert entry.file_sha256 == expected_hash
        assert manifest.has_hash(expected_hash)

        # 2. Second download with same URL / content -> is_new == False
        saved_path_2, entry_2, is_new_2 = download_dpi_pdf(
            url,
            target_dir=download_dir,
            client=client,
            manifest=manifest,
        )

        assert is_new_2 is False
        assert saved_path_2 == saved_path
        assert entry_2.file_sha256 == expected_hash

    def test_sync_portal_pdfs_integration(self, tmp_path: Path):
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if url_str.endswith(".pdf"):
                return httpx.Response(
                    200, content=f"%PDF-1.4 Unique Content for {url_str}".encode()
                )
            return httpx.Response(200, text=SAMPLE_PORTAL_HTML)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        manifest = ManifestManager(path=tmp_path / "manifest.json")

        results = sync_portal_pdfs(
            limit=2,
            target_dir=tmp_path / "raw",
            manifest=manifest,
            client=client,
        )

        # Requested limit 2
        assert len(results) == 2
        assert len(manifest.all_entries()) == 2
