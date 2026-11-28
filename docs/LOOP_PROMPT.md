# Improvement Loop Prompt

Start the loop from the repo root with:

```
/loop Read docs/LOOP_PROMPT.md and carry out exactly one iteration as it describes.
```

(Self-paced; add an interval such as `/loop 30m ...` for fixed pacing.) Everything below is the
instruction set for each iteration.

---

## Role

You are the maintainer of DAPriceScraper, an unofficial, free, read-only API and website for
Philippine Department of Agriculture Daily Price Index data. Each iteration you complete **one**
item from [docs/IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) to its Definition of Done, record it,
and commit it. When every item is finished, you extend the plan, then keep going.

The plan file is the single source of truth and your memory between iterations. Do not rely on
remembering earlier iterations; re-read the plan and `git log` every time.

## Environment facts

- Windows, PowerShell primary (Git Bash also available). Python ≥ 3.11. Package in `src/da_price_index/`.
- Install/refresh deps: `pip install -e ".[dev]"`.
- Checks: `ruff check .` · `python run_tests.py` · `python -m pytest -q`.
- Local server with real data (never touches the DA portal):
  ```
  $env:DA_DPI_DB_URL = "sqlite:///<scratch>/loop.db"
  da-dpi sync --file tests/fixtures/Daily-Price-Index-November-22-2026.pdf   # repeat for each fixture, oldest first
  da-dpi serve --port 8765
  ```
  Use a scratch directory for the DB, run the server in the background, verify with
  `curl`/`Invoke-WebRequest`, and stop it when done.
- Site files: `src/da_price_index/site/` (served at `/`, `/api-docs`, `/static/*`).
  MCP tools: `src/da_price_index/mcp_server.py`. API: `src/da_price_index/api.py`.

## Hard rules

1. **One item per iteration.** Do not start a second item in the same iteration.
2. **Never contact the DA portal** (`da.gov.ph`) or run `da-dpi fetch`, `da-dpi sync` without
   `--file`, or `da-dpi run-daily`. Use fixtures only.
3. **Git:** work on branch `improvement-loop` (create it from the current `HEAD` on the first
   iteration). One commit per item, message `<ID>: <title>` plus a short body, ending with the
   `Co-Authored-By` trailer the harness specifies. Never commit to `main`, never push, never force,
   never rewrite history, never use `--no-verify`.
4. **Backward compatibility.** Never remove or rename an existing endpoint, parameter, response
   field, CLI command or env var, and never change a default. If an item can't be done without
   that, mark it `[?]` with a proposal and move on.
5. **Stay inside the guiding principles** in section 1 of the plan (no build step, no CDNs, no
   tracking, no write endpoints, single replica, minimal dependencies).
6. **Don't weaken tests** to make them pass. Don't delete or skip existing tests. If an existing
   test is genuinely wrong, fix it and explain why in the commit body.
7. **No secrets** in code, logs, commits or docs. Don't edit `.env` files or CI secrets.
8. **Don't touch the parser** (`parser.py`, `validators.py`, `scraper.py`) unless the current item
   explicitly requires it.

## Iteration procedure

### Step 0 — Orient
1. Read `docs/IMPROVEMENT_PLAN.md` fully (a human may have edited it).
2. `git status`, `git branch --show-current`, `git log --oneline -10`.
3. Preflight:
   - If you're on `main` and the working tree has uncommitted changes that aren't from this
     loop, **stop the loop** (see Stop conditions) and ask the user to commit or stash them first.
     Never stash, reset or discard them yourself.
   - If you're on `improvement-loop` with uncommitted changes, they're from an interrupted
     iteration: find the `[~]` item, review the diff, and either finish it or revert only your
     own partial work for it, then continue.
   - Switch to `improvement-loop` (create it if missing).
4. Run `ruff check .` and `python run_tests.py`. If the baseline is red, your item for this
   iteration is to fix the baseline (log it as `baseline-fix`), not a plan item.

### Step 1 — Pick the item
- If an item is `[~]`, resume it.
- Otherwise pick the first `[ ]` item whose `Depends:` are all `[x]`, ordered by: lowest phase
  number → priority (P0, then P1, then P2) → order in the file.
- If there's no eligible item, go to **Step 6 — Expand**.
- Mark it `[~]` in the plan before writing code.

### Step 2 — Understand
- Read every file the item touches, plus its tests. Find the existing patterns (ETag helpers,
  `create_app(repo=..., rate_limiter=...)`, test fixtures in `tests/test_api.py`) and follow them.
- If the item is ambiguous, choose the simplest reading that satisfies the acceptance criteria
  and the principles, and write that choice down in the work log note.
- If the item is too large for one iteration, split it in the plan into `<ID>a`, `<ID>b`, …
  (each with acceptance criteria), then do only `<ID>a`.

### Step 3 — Implement
- Write the tests first where practical, see them fail, then implement.
- Keep the change focused: no drive-by refactors outside the item (note them in the Backlog).
- For any data-facing API change, update the MCP tools if the item says so or parity requires it.
- Update docs in the same change: `site/api-docs.html` (endpoint article, nav link, and a
  Changelog entry), `README.md` endpoint list/config table, and OpenAPI descriptions.
- If you add a test class or file, make sure `python run_tests.py` actually runs it (until C7
  is done, that means adding it to the list in `run_tests.py`).

### Step 4 — Verify (all required)
1. `ruff check .` — clean.
2. `python run_tests.py` and `python -m pytest -q` — all pass. Paste the summary line in your
   final message.
3. Run it for real: start the local server seeded from the fixtures (see Environment), hit
   every endpoint or page you changed, and check status codes, headers and bodies. For site
   items, fetch the HTML/JS/CSS, check the new markup and that `site.js` has no syntax errors
   (`node --check` if Node is available), and think through 360px width, dark theme, keyboard
   use, and API failure states. Stop the server afterwards.
4. Re-read your diff (`git diff`) against the item's acceptance criteria and the Definition of
   Done. Fix anything missing before continuing.

If you can't get verification green after a reasonable effort, revert only this item's changes,
mark it `[!]` with the concrete blocker (error message, what you tried), log it, and end the
iteration.

### Step 5 — Record & commit
1. In the plan: change the item to `[x]` (or `[!]`/`[?]`), add a short "Done:" note under it if
   the result differs from what the item described, and move any discoveries to the Backlog.
2. Append to the Work log: `YYYY-MM-DD · <ID> · done · <short hash or "pending"> · <note>`.
3. Commit code and plan together on `improvement-loop`. Then amend nothing; if you want the hash
   in the log, put it in the next iteration's log entry instead.
4. Final message for the iteration (shown to the user): item ID and title, what changed (files),
   test summary line, anything marked `[!]`/`[?]`, and the next eligible item.

### Step 6 — Expand (only when no item is eligible)
1. Audit for evidence, reading actual code and output rather than guessing:
   - `[!]` and `[?]` items: can a smaller unblocked follow-up be done?
   - Run the test suite with coverage (if available) and list the least-covered modules.
   - Read `api.py`, `db.py`, `mcp_server.py`, `site/*` for bugs, duplication, missing
     validation, inconsistent docs, a11y gaps.
   - Time the main endpoints against the seeded local server.
   - Re-check the Backlog for ideas that now fit the principles.
2. Append `## Phase N — <theme>` with 5–10 new items following the **Expansion rules** in the
   plan (evidence-cited, acceptance criteria, priority, dependencies, one iteration each).
3. Log the expansion with a one-line summary of the evidence, commit the plan alone
   (`plan: add phase N`), and end the iteration. Implementation starts next iteration.
4. If the audit finds nothing worth doing that respects the principles, don't invent filler:
   log "no further improvements found" and stop the loop.

## Stop conditions (end the loop with `ScheduleWakeup` `stop: true` and explain why)

- The working tree on `main` has user changes (preflight rule).
- Three iterations in a row ended with `[!]` or a red baseline you couldn't fix.
- Every remaining item is `[?]` (needs a human decision). List them for the user.
- Expansion found nothing worth doing.
- The user asks you to stop.

## Pacing

When an iteration ends normally, schedule the next wakeup soon (about 60–120 s); nothing
external is being waited on. Set `noop: false` after an iteration that completed or expanded
something, `noop: true` only if the iteration found nothing to do.
