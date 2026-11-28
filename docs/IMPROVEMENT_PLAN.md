# DAPriceScraper — Site & API Improvement Plan

A living plan. The improvement loop (see [LOOP_PROMPT.md](LOOP_PROMPT.md)) works through it one
item at a time, ticks items off, logs what it did, and appends new phases when every item is done.
Humans may edit it at any time; the loop re-reads it at the start of every iteration.

---

## 1. Guiding principles (constraints every item must respect)

1. **Backward compatible API.** Changes are additive. Existing paths, parameters, field names,
   default behaviors and response shapes keep working. A breaking change is never implemented by
   the loop; it is marked `[?]` (needs a human decision) with a short proposal.
2. **Free, open, read-only.** No API keys, accounts, write endpoints or tracking. CORS stays open
   for reads.
3. **Polite to the DA.** Nothing increases how often the DA portal is contacted. Tests and local
   verification use the PDFs in `tests/fixtures/`, never the live portal.
4. **No build step for the site.** Plain HTML/CSS/JS in `src/da_price_index/site/`, no framework,
   no external CDN or fonts, no analytics. Charts are inline SVG drawn by `site.js`.
5. **Single replica.** In-memory state (rate limiter, caches) is fine; document it where it matters.
6. **Small dependency footprint.** A new runtime dependency needs a one-line justification in the
   work log. Prefer the standard library, FastAPI/Starlette built-ins and SQLAlchemy.
7. **Every change is tested and documented.** Tests in `tests/`, user-facing docs in `README.md`
   and `site/api-docs.html` (including its Changelog section), and the MCP server kept at parity
   where the change is data-facing.
8. **Unofficial and honest.** The site never implies DA affiliation; data provenance stays visible.

## 2. Definition of done (applies to every item)

- Acceptance criteria in the item are all met.
- `ruff check .` passes; `python run_tests.py` and `python -m pytest -q` both pass.
- New behavior has tests (API via `fastapi.testclient`, DB via SQLite temp files, site via
  served-HTML assertions in `TestSite`).
- New/changed endpoints are documented in `site/api-docs.html` (endpoint article + changelog
  entry) and `README.md` endpoint list; OpenAPI has descriptions and an example.
- Site changes work at 360px width, in light and dark themes, with keyboard only, and degrade
  gracefully when the API returns 404/429/500 or JS fails.
- The item is ticked `[x]` here and a line is added to the Work log.

## 3. Status legend

`[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked (reason in item) · `[?]` needs human decision

Priority: **P0** fix/foundation · **P1** high value · **P2** nice to have.
Items may list `Depends:`; an item is only eligible when its dependencies are `[x]`.

---

## Phase 1 — API correctness & hygiene

- [ ] **A1 (P0) Validate `format` and content negotiation.**
  `format` on `/prices/latest` and `/prices` accepts only `json` | `csv` (use a `Literal`/Enum so
  OpenAPI shows it); anything else → 422. Extract one helper `wants_csv(request, format)` used by
  every endpoint. Accept header `text/csv` keeps working.
  *Accept:* `?format=xml` → 422; `?format=CSV` handled consistently (decide: case-insensitive);
  tests for both paths on both endpoints.

- [ ] **A2 (P0) Cheap 304s on `/prices/latest`.**
  Compute the ETag from `get_latest_report_info()` + request parameters *before* running
  `get_latest_known_prices()` (as `/prices/summary` already does), and return 304 without the
  heavy query. Extract a shared `etag_for(*parts)` + `cache_headers(etag)` helper and use it in
  both endpoints.
  *Accept:* a test proves `get_latest_known_prices` is not called on a matching `If-None-Match`;
  existing ETag values may change, but must stay stable for identical requests.

- [ ] **A3 (P0) Caching headers on the remaining read endpoints.**
  Add ETag + `Cache-Control` + 304 support to `/prices` (history), `/commodities`,
  `/reports/{date}` and `/health` (`/health`: `no-cache` only). ETags derive from the latest
  report id + parser version + normalized query params. `If-None-Match` handles lists and `*`.
  Depends: A2.
  *Accept:* tests for 200→304 round-trip on each endpoint; ETag differs when params differ.

- [ ] **A4 (P0) Region handling.**
  Region codes are case-insensitive (`ncr` → `NCR`) everywhere, including MCP tools. An unknown
  region returns 404 whose body lists the known regions.
  *Accept:* tests for lowercase region and unknown region on every region-aware endpoint.

- [ ] **A5 (P0) Machine-readable errors.**
  Keep FastAPI's `detail` field for compatibility, and add `code` (e.g. `not_found`,
  `invalid_parameter`, `rate_limited`, `no_data`) plus `status` to every error body, including
  422 validation errors and the rate limiter's 429. Document the codes in the Errors section of
  `api-docs.html`.
  *Accept:* tests assert `detail` and `code` on 404, 422 and 429.

- [ ] **A6 (P0) Input validation on history.**
  `/prices`: `from > to` → 422; non-positive `commodity_id` → 422. `/reports/{date}` keeps 404 for
  missing dates.
  *Accept:* tests for each invalid case.

- [ ] **A7 (P0) Database indexes for read paths.**
  Add `migrations/003_read_indexes.sql` and matching SQLAlchemy `Index` definitions:
  `daily_commodity_prices(commodity_id, report_date)` and `daily_commodity_prices(report_id)`.
  `create_tables()` must create them on existing databases (idempotent).
  *Accept:* a test inspects the SQLite schema and finds both indexes; existing DBs upgrade cleanly.

- [ ] **A8 (P1) Better `/health`.**
  Add a DB connectivity check (503 with `status: "degraded"` if the DB query fails), the latest
  report per region, `data_age_days`, `stale: true` when the newest report is older than
  `DA_DPI_STALE_AFTER_DAYS` (default 3), and the last `run-daily` job result from `job_runs`.
  Existing fields stay unchanged.
  *Accept:* tests for healthy, stale and DB-down states.

## Phase 2 — New API capabilities

- [ ] **B1 (P1) `GET /categories`.** REST parity with MCP `list_categories`: top categories with
  sub-categories and commodity counts. JSON only. *Accept:* tests; docs; ETag.

- [ ] **B2 (P1) `GET /regions`.** Regions present in the DB with name, report count, first/latest
  report date. *Accept:* tests; docs.

- [ ] **B3 (P1) `GET /reports` and `GET /reports/latest`.** Paginated list of reports (newest
  first; `region`, `from`, `to`, `limit` ≤ 366, `offset`) with provenance fields but without the
  full market list; `/reports/latest` returns the same shape as `/reports/{date}`. Make sure the
  route order keeps `/reports/{date}` working.
  *Accept:* tests including pagination edges and the route-ordering case.

- [ ] **B4 (P1) `GET /commodities/{id}`.** One commodity with its aliases, its latest price, and a
  30-day summary block. 404 for unknown ids. *Accept:* tests; MCP not required.

- [ ] **B5 (P1) `GET /prices/changes` (movers).** Per commodity: latest price, the price `days`
  ago (default 1, i.e. previous report; 1–366), absolute and percentage change, direction.
  Filters: `region`, `category`, `commodity_id[]`, `min_abs_pct`; `sort=abs_pct|pct|name`;
  `limit`. JSON + CSV. Compare to the closest available report on or before the target date, and
  say which date was used. Add an MCP tool `get_price_changes` with the same semantics.
  *Accept:* tests with fixtures covering up, down, unchanged, missing-baseline and N/A cases.

- [ ] **B6 (P1) Multi-commodity history.** `/prices` accepts repeated `commodity_id` (≤ 20).
  A single id behaves exactly as today. CSV works. *Accept:* tests for 1, many and >20 ids.

- [ ] **B7 (P2) Pagination on list endpoints.** `limit`/`offset` on `/commodities`, `/prices`,
  `/reports` with an RFC 8288 `Link` header and `X-Total-Count`. Defaults return everything
  (today's behavior) so nothing breaks. *Accept:* tests for `Link` header correctness.

- [ ] **B8 (P2) CSV everywhere.** `format=csv` on `/prices/summary`, `/commodities`,
  `/prices/changes`, `/reports`. One shared CSV writer; RFC 4180; UTF-8 with stable column order.
  *Accept:* tests parse the CSV with `csv` module and check headers.

- [ ] **B9 (P1) Rate-limit headers.** Every rate-limited response carries `RateLimit-Limit`,
  `RateLimit-Remaining`, `RateLimit-Reset` for the tightest window (IETF draft fields), exposed via
  CORS. *Accept:* tests on 200 and 429; documented in Rate limits section.

- [ ] **B10 (P2) MCP parity review.** Every REST capability added in this phase that answers a
  natural-language question has an MCP tool or is reachable through an existing one. Tool
  descriptions mention units and the "prevailing price" meaning. *Accept:* `tests/test_mcp.py`
  covers each new tool.

## Phase 3 — Architecture, performance & operations

- [ ] **C1 (P1) Service layer.** Move response shaping shared by `api.py` and `mcp_server.py`
  (row→dict mapping, ETag/caching helpers, region normalization, CSV selection) into
  `src/da_price_index/service.py`. `db.py` keeps queries only. No behavior change.
  *Accept:* all tests pass unchanged; `api.py` shrinks; duplicated row-mapping in `db.py`
  (`get_latest_known_prices` / `get_price_history`) collapses into one helper.

- [ ] **C2 (P1) Gzip.** Add Starlette `GZipMiddleware` (minimum size ~1 KB) in the right place
  relative to CORS and rate limiting. Weak ETags remain valid. *Accept:* test that a large
  `/prices/latest` response is gzipped when `Accept-Encoding: gzip` is sent and not otherwise.

- [ ] **C3 (P2) In-process response cache.** Cache rendered bodies of `/prices/latest`,
  `/prices/summary`, `/prices/changes`, `/categories` keyed by (endpoint, normalized params,
  latest report id, parser version), bounded LRU. A new report invalidates naturally because the
  key changes. *Accept:* test shows the second identical request does not hit the repository;
  test shows a new report changes the response.

- [ ] **C4 (P1) Performance budget test.** A test helper seeds SQLite with 365 days × 200
  commodities (synthetic, fast bulk insert). A test file (marked so it can be skipped with
  `-m "not perf"`) asserts generous budgets (e.g. `/prices/latest` < 500 ms, `/prices/summary`
  < 800 ms on SQLite). Record measured numbers in the work log. *Accept:* budgets pass in CI.

- [ ] **C5 (P1) Request IDs and access logs.** Middleware sets/propagates `X-Request-ID`, logs one
  structured line per request (method, path, status, duration ms, client IP hash, request id).
  No raw IPs in logs. *Accept:* tests for header propagation.

- [ ] **C6 (P1) Security headers.** For HTML responses: `Content-Security-Policy` (self only for
  `/` and `/api-docs`; a separate policy for `/docs` that allows exactly what FastAPI's Swagger UI
  loads), `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `frame-ancestors 'none'`. API JSON gets `nosniff`. *Accept:* tests; `/docs` still renders
  (check the HTML references resolve under the policy).

- [ ] **C7 (P0) One test runner.** Make `run_tests.py` delegate to pytest (`pytest.main`) so new
  test classes can never be forgotten; keep the command working for CI and README. Add
  `pytest-cov` to dev deps and report coverage in CI (no hard threshold yet; record the baseline
  in the work log). *Accept:* `python run_tests.py` runs every test discovered by pytest.

- [ ] **C8 (P2) Deployed smoke test.** `scripts/smoke.py BASE_URL` hits every public endpoint and
  site page, checks status codes, content types, CORS and rate-limit headers, and exits non-zero
  on failure. Documented in README. No secrets. *Accept:* runs green against a local `da-dpi serve`
  seeded from fixtures.

## Phase 4 — Website

- [ ] **D1 (P1) Sortable, shareable price table.** Click/Enter on column headers sorts (with
  `aria-sort`); search and category are reflected in the URL (`?q=&cat=&sort=`) and restored on
  load; a "Copy link" button. *Accept:* manual check documented in work log + served-HTML test for
  the new controls.

- [ ] **D2 (P1) Price-change column.** Using `/prices/changes`, show ▲/▼ with % vs previous report
  (color + symbol, never color alone), sortable. Depends: B5, D1.

- [ ] **D3 (P1) Commodity detail view.** Clicking a row opens `/commodity/{id}` (a new static page
  served by FastAPI, or a hash route — pick the simpler one and note why) with: name/spec/unit,
  latest price, 30/90/365-day min/median/max, an inline-SVG line chart of history with accessible
  text summary, a CSV download link, and the API call that produced it. Depends: B4.

- [ ] **D4 (P1) Theme toggle.** Light / dark / system toggle in the header using the existing
  `data-theme` CSS hooks; persisted in `localStorage` (wrapped in try/catch); no flash of the wrong
  theme on load. *Accept:* works on both pages.

- [ ] **D5 (P1) Freshness banner.** If `/health` reports `stale: true`, show a dismissible notice
  ("Latest DA report is N days old"). Depends: A8.

- [ ] **D6 (P1) Accessibility pass.** Skip link, table `<caption>`, `aria-live` on the result count,
  `role="tabpanel"`/`aria-controls` wiring for code tabs, visible focus styles, contrast ≥ 4.5:1 for
  text in both themes (check the token pairs and record results), reduced-motion respected.

- [ ] **D7 (P1) Loading, error and empty states.** Skeleton rows while loading; a retry button on
  failure; a specific message for 429 (with Retry-After); the stats row never shows blank values.

- [ ] **D8 (P2) Mobile layout.** Under 600px the price table becomes stacked cards (name, spec,
  price, change) without horizontal page scroll; tap targets ≥ 44px.

- [ ] **D9 (P1) SEO & sharing.** `/robots.txt`, `/sitemap.xml`, `<link rel="canonical">`, an OG image
  (`/static/og.png` or SVG), and JSON-LD `Dataset` metadata on `/` that describes the data and
  credits the DA as the source. Exempt these paths from rate limiting.

- [ ] **D10 (P2) Static asset caching.** Version static asset URLs (e.g. `?v=<package version>`
  injected at serve time, or content hash) and serve `/static/*` with long `Cache-Control`
  + `immutable`; HTML stays short-cache.

- [ ] **D11 (P1) Docs/site stay in sync automatically.** A test fails if any public OpenAPI path is
  missing from `api-docs.html`, if the docs mention a path that does not exist, or if any internal
  link/anchor on `/` or `/api-docs` is broken. Then document all Phase 2 endpoints.

- [ ] **D12 (P2) "Build your request" helper.** On `/api-docs`, a small form (endpoint, region,
  commodity search, format) that builds the URL, shows cURL/JS/Python snippets, and has a "Run"
  button that shows the live response (truncated).

## Phase 5 — Developer experience & contract safety

- [ ] **E1 (P1) Rich OpenAPI.** Every endpoint has a summary, description, response examples
  (from real fixture data), documented error responses, and stable `operationId`s
  (e.g. `getLatestPrices`). Tag descriptions on the docs page.

- [ ] **E2 (P1) OpenAPI contract snapshot.** `tests/snapshots/openapi.json` plus a test that fails
  on any removed path/parameter/field or changed type (additions pass and print a reminder to
  refresh the snapshot with a documented command). Depends: E1.

- [ ] **E3 (P2) Versioned alias.** Serve every API path also under `/v1/...` (same handlers), and
  document `/v1` as the stable prefix; un-prefixed paths keep working indefinitely.

- [ ] **E4 (P2) Example notebook/script.** `examples/price_trends.py` using only `httpx` that pulls
  a summary and prints the top movers, referenced from the docs.

---

## Backlog (unprioritized ideas; the loop may promote these during expansion)

- Weekly/monthly aggregates endpoint (`/prices/aggregate?period=week`).
- Atom/RSS feed of new reports (`/reports/feed.xml`).
- Price-alert webhooks — **conflicts with read-only principle; needs human decision.**
- Basket/"cost of a meal" calculator page built on `/prices/latest`.
- Comparison view of several commodities on one chart.
- Embeddable price widget (`<iframe>` or script) for blogs/news.
- Translations (Filipino) for the site.

## Expansion rules (used by the loop when every item above is `[x]`, `[!]` or `[?]`)

Append a new phase `Phase N — <theme>` with 5–10 items in the same format. Items must:
- be grounded in evidence gathered that iteration (a code audit finding, test gap, slow query,
  doc inconsistency, a11y issue, backlog idea, or `[!]`/`[?]` follow-ups), cited in the item;
- respect the guiding principles; never duplicate an existing item;
- be small enough for one iteration each (split otherwise);
- include acceptance criteria.
Record the expansion in the work log with the evidence summary.

---

## Work log

Newest last. Format: `YYYY-MM-DD · <item> · <status> · <commit> · <one-line note>`

- 2026-11-28 · plan · created · — · Initial plan written from a code audit of api.py, db.py,
  schemas.py, ratelimit.py, mcp_server.py and site/.
