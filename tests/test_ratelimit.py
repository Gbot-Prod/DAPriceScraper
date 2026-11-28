"""Tests for per-IP rate limiting and API hardening (CORS)."""

from pathlib import Path

from fastapi.testclient import TestClient

from da_price_index.api import create_app
from da_price_index.db import DatabaseRepository
from da_price_index.ratelimit import RateLimiter, Window


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestRateLimiter:
    """Unit tests for the fixed-window limiter."""

    def test_minute_window_blocks_then_resets(self, tmp_path: Path | None = None):
        clock = FakeClock(600.0)  # aligned to a minute boundary
        limiter = RateLimiter([Window("minute", 60, 3)], clock=clock)

        assert [limiter.hit("1.1.1.1")[0] for _ in range(3)] == [True, True, True]
        allowed, retry_after = limiter.hit("1.1.1.1")
        assert allowed is False
        assert retry_after == 60

        clock.now += 45
        assert limiter.hit("1.1.1.1") == (False, 15)

        clock.now += 15  # next minute
        assert limiter.hit("1.1.1.1")[0] is True

    def test_clients_are_independent(self, tmp_path: Path | None = None):
        limiter = RateLimiter([Window("minute", 60, 1)], clock=FakeClock())
        assert limiter.hit("1.1.1.1")[0] is True
        assert limiter.hit("1.1.1.1")[0] is False
        assert limiter.hit("2.2.2.2")[0] is True

    def test_longer_window_enforced_across_minutes(self, tmp_path: Path | None = None):
        clock = FakeClock(3600.0)
        limiter = RateLimiter([Window("minute", 60, 2), Window("hour", 3600, 3)], clock=clock)
        assert limiter.hit("ip")[0] and limiter.hit("ip")[0]
        clock.now += 60
        assert limiter.hit("ip")[0] is True
        allowed, retry_after = limiter.hit("ip")  # minute ok, hour exhausted
        assert allowed is False
        assert retry_after == 3600 - 60

    def test_rejected_requests_do_not_consume_quota(self, tmp_path: Path | None = None):
        clock = FakeClock(3600.0)
        limiter = RateLimiter([Window("minute", 60, 1), Window("hour", 3600, 2)], clock=clock)
        assert limiter.hit("ip")[0] is True
        for _ in range(10):
            assert limiter.hit("ip")[0] is False  # blocked by minute window
        clock.now += 60
        assert limiter.hit("ip")[0] is True  # hour quota not burned by rejects

    def test_zero_limit_disables_window(self, tmp_path: Path | None = None):
        limiter = RateLimiter([Window("minute", 60, 0)], clock=FakeClock())
        assert all(limiter.hit("ip")[0] for _ in range(100))

    def test_expired_counters_are_pruned(self, tmp_path: Path | None = None):
        clock = FakeClock(0.0)
        limiter = RateLimiter([Window("minute", 60, 5)], clock=clock)
        for i in range(50):
            limiter.hit(f"10.0.0.{i}")
        clock.now += 120
        limiter.hit("10.0.1.1")
        assert len(limiter._counts) == 1


class TestRateLimitMiddleware:
    """Integration tests through the FastAPI app."""

    def _client(self, tmp_path: Path, per_minute: int) -> tuple[TestClient, DatabaseRepository]:
        repo = DatabaseRepository(db_url=f"sqlite:///{tmp_path}/rl.db")
        repo.create_tables()
        limiter = RateLimiter([Window("minute", 60, per_minute)])
        return TestClient(create_app(repo, rate_limiter=limiter)), repo

    def test_returns_429_with_retry_after_per_ip(self, tmp_path: Path):
        client, repo = self._client(tmp_path, per_minute=2)
        try:
            a = {"X-Real-IP": "203.0.113.5"}
            assert client.get("/commodities", headers=a).status_code == 200
            assert client.get("/commodities", headers=a).status_code == 200
            blocked = client.get("/commodities", headers=a)
            assert blocked.status_code == 429
            assert int(blocked.headers["retry-after"]) >= 1
            assert blocked.json()["detail"].startswith("Rate limit exceeded")

            # A different client IP is unaffected
            b = {"X-Real-IP": "198.51.100.7"}
            assert client.get("/commodities", headers=b).status_code == 200
        finally:
            repo.close()

    def test_health_is_exempt(self, tmp_path: Path):
        client, repo = self._client(tmp_path, per_minute=1)
        try:
            for _ in range(5):
                assert client.get("/health").status_code == 200
        finally:
            repo.close()

    def test_site_pages_and_assets_are_exempt(self, tmp_path: Path):
        client, repo = self._client(tmp_path, per_minute=1)
        try:
            for _ in range(3):
                for path in ("/", "/api-docs", "/static/site.css", "/docs", "/openapi.json"):
                    assert client.get(path).status_code == 200, path
            # Data endpoints are still limited
            assert client.get("/commodities").status_code == 200
            assert client.get("/commodities").status_code == 429
        finally:
            repo.close()

    def test_429_carries_cors_headers(self, tmp_path: Path):
        client, repo = self._client(tmp_path, per_minute=1)
        try:
            headers = {"X-Real-IP": "203.0.113.9", "Origin": "https://example.com"}
            client.get("/commodities", headers=headers)
            blocked = client.get("/commodities", headers=headers)
            assert blocked.status_code == 429
            assert blocked.headers["access-control-allow-origin"] == "*"
        finally:
            repo.close()

    def test_cors_does_not_allow_credentials(self, tmp_path: Path):
        client, repo = self._client(tmp_path, per_minute=100)
        try:
            response = client.get("/health", headers={"Origin": "https://example.com"})
            assert response.headers["access-control-allow-origin"] == "*"
            assert "access-control-allow-credentials" not in response.headers
        finally:
            repo.close()
