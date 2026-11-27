-- DA Price Index — scheduled job run log
-- Used by `da-dpi run-daily` to count consecutive failures for alerting.
-- (The application also creates this table automatically on startup.)
--
-- SQLite mapping: BIGSERIAL → INTEGER PRIMARY KEY AUTOINCREMENT,
--                 TIMESTAMPTZ → TEXT, BOOLEAN → INTEGER (0/1)

CREATE TABLE IF NOT EXISTS job_runs (
    run_id       BIGSERIAL PRIMARY KEY,
    job_name     VARCHAR(64) NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ NOT NULL,
    succeeded    BOOLEAN NOT NULL,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS ix_job_runs_job_name_run_id ON job_runs (job_name, run_id);
