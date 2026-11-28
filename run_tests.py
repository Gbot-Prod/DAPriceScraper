"""Standalone test runner for test_parser and test_validators."""

import sys
from pathlib import Path

# Add src and tests to sys.path
sys.path.insert(0, str(Path("src").resolve()))
sys.path.insert(0, str(Path("tests").resolve()))

from test_api import TestAPI, TestPriceSummary, TestSite
from test_cli import TestCLI
from test_db import TestDatabaseRepository, TestExporters, TestReportDateCorrection
from test_mcp import TestMCP
from test_parser import TestDPIParser, TestReportDateReconciliation
from test_ratelimit import TestRateLimiter, TestRateLimitMiddleware
from test_run_daily import TestDbUrlNormalization, TestRunDaily
from test_scraper import TestDownloader, TestManifestManager, TestScraperDiscovery
from test_validators import TestValidators


def run_all():
    import inspect
    import tempfile
    passed = 0
    failed = 0

    test_classes = [
        ("TestDPIParser", TestDPIParser),
        ("TestReportDateReconciliation", TestReportDateReconciliation),
        ("TestValidators", TestValidators),
        ("TestScraperDiscovery", TestScraperDiscovery),
        ("TestManifestManager", TestManifestManager),
        ("TestDownloader", TestDownloader),
        ("TestDatabaseRepository", TestDatabaseRepository),
        ("TestExporters", TestExporters),
        ("TestReportDateCorrection", TestReportDateCorrection),
        ("TestCLI", TestCLI),
        ("TestAPI", TestAPI),
        ("TestPriceSummary", TestPriceSummary),
        ("TestSite", TestSite),
        ("TestMCP", TestMCP),
        ("TestRateLimiter", TestRateLimiter),
        ("TestRateLimitMiddleware", TestRateLimitMiddleware),
        ("TestRunDaily", TestRunDaily),
        ("TestDbUrlNormalization", TestDbUrlNormalization),
    ]

    for class_name, cls in test_classes:
        print(f"\nRunning {class_name}...")
        instance = cls()
        for attr in dir(instance):
            if attr.startswith("test_"):
                test_fn = getattr(instance, attr)
                try:
                    print(f"  • {attr} ... ", end="", flush=True)
                    sig = inspect.signature(test_fn)
                    if len(sig.parameters) > 0:
                        with tempfile.TemporaryDirectory() as td:
                            test_fn(Path(td))
                    else:
                        test_fn()
                    print("PASSED")
                    passed += 1
                except Exception as e:
                    print(f"FAILED: {e}")
                    import traceback
                    traceback.print_exc()
                    failed += 1

    print("\n" + "=" * 50)
    print(f"TOTAL: {passed} passed, {failed} failed")
    print("=" * 50)
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
