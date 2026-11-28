"""Tests for the MCP endpoint (JSON-RPC over Streamable HTTP at /mcp)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from da_price_index.api import create_app
from da_price_index.db import DatabaseRepository
from da_price_index.parser import parse_dpi_pdf
from da_price_index.ratelimit import RateLimiter, Window

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PDFS = [
    "Daily-Price-Index-November-22-2026.pdf",
    "Daily-Price-Index-November-23-2026.pdf",
    "November-24-2026-DPI-AFC.pdf",
    "Daily-Price-Index-November-25-2026.pdf",
]
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _repo(tmp_path: Path, with_data: bool = True) -> DatabaseRepository:
    repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/mcp.db")
    repo.create_tables()
    if with_data:
        for name in PDFS:
            repo.upsert_report(parse_dpi_pdf(FIXTURES_DIR / name))
    return repo


def _rpc(client: TestClient, method: str, params: dict[str, Any] | None = None) -> dict:
    res = client.post(
        "/mcp",
        headers=HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert res.status_code == 200, res.text
    return res.json()


def _call(client: TestClient, tool: str, **arguments: Any) -> dict:
    return _rpc(client, "tools/call", {"name": tool, "arguments": arguments})["result"]


class TestMCP:
    def test_initialize_and_list_tools(self, tmp_path: Path):
        repo = _repo(tmp_path, with_data=False)
        try:
            with TestClient(create_app(repo)) as client:
                init = _rpc(
                    client,
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                )["result"]
                assert init["serverInfo"]["name"] == "DAPriceScraper"
                assert "search_commodities" in init["instructions"]

                tools = {t["name"]: t for t in _rpc(client, "tools/list")["result"]["tools"]}
                assert set(tools) == {
                    "search_commodities",
                    "list_categories",
                    "get_latest_prices",
                    "get_price_summary",
                    "get_price_history",
                    "get_report_info",
                }
                for tool in tools.values():
                    assert tool["description"]
                    assert tool["annotations"]["readOnlyHint"] is True
        finally:
            repo.close()

    def test_tools_return_prices(self, tmp_path: Path):
        repo = _repo(tmp_path)
        try:
            with TestClient(create_app(repo)) as client:
                found = _call(client, "search_commodities", query="liempo")["structuredContent"]
                assert found["total_matches"] >= 1
                ids = [c["commodity_id"] for c in found["commodities"]]
                assert all("Liempo" in c["name"] for c in found["commodities"])

                cats = _call(client, "list_categories")["structuredContent"]["categories"]
                assert any(c["category"] == "LIVESTOCK & POULTRY PRODUCTS" for c in cats)

                latest = _call(client, "get_latest_prices", commodity_ids=[*ids, 999999])
                latest = latest["structuredContent"]
                assert latest["latest_report_date"] == "2026-11-25"
                assert {p["commodity_id"] for p in latest["prices"]} == set(ids)
                assert latest["not_found"] == [999999]
                assert all(p["price"] is None or "." in p["price"] for p in latest["prices"])

                summary = _call(client, "get_price_summary", commodity_ids=ids, days=7)
                summary = summary["structuredContent"]
                assert summary["window_end"] == "2026-11-25"
                for s in summary["summaries"]:
                    assert float(s["min"]) <= float(s["median"]) <= float(s["max"])

                history = _call(client, "get_price_history", commodity_id=ids[0])
                history = history["structuredContent"]["history"]
                assert [h["date"] for h in history] == sorted(h["date"] for h in history)
                assert len(history) == 4

                report = _call(client, "get_report_info")["structuredContent"]
                assert report["report_date"] == "2026-11-25"
                assert report["market_count"] == len(report["markets_surveyed"]) > 0
        finally:
            repo.close()

    def test_errors_reach_the_model(self, tmp_path: Path):
        repo = _repo(tmp_path, with_data=False)
        try:
            with TestClient(create_app(repo)) as client:
                cases = [
                    ("get_latest_prices", {"commodity_ids": []}, "search_commodities"),
                    ("get_price_history", {"commodity_id": 42}, "No commodity with id 42"),
                    ("get_report_info", {"report_date": "nope"}, "YYYY-MM-DD"),
                    ("get_price_summary", {"commodity_ids": [1], "days": 0}, "between 1 and 366"),
                    ("get_latest_prices", {"commodity_ids": [1]}, "No price reports"),
                ]
                for tool, args, expected in cases:
                    result = _call(client, tool, **args)
                    assert result["isError"] is True, tool
                    assert expected in result["content"][0]["text"], (tool, result)
        finally:
            repo.close()

    def test_mcp_is_rate_limited_and_cors_allows_post(self, tmp_path: Path):
        repo = _repo(tmp_path, with_data=False)
        limiter = RateLimiter([Window("minute", 60, 1)])
        try:
            with TestClient(create_app(repo, rate_limiter=limiter)) as client:
                _rpc(client, "tools/list")
                blocked = client.post(
                    "/mcp",
                    headers=HEADERS,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                )
                assert blocked.status_code == 429

                preflight = client.options(
                    "/mcp",
                    headers={
                        "Origin": "https://inspector.example",
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "content-type,mcp-protocol-version",
                    },
                )
                assert preflight.status_code == 200
                assert "POST" in preflight.headers["access-control-allow-methods"]
        finally:
            repo.close()
