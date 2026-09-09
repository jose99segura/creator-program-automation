-- Every table here is written by exactly one step, and read by the next.
-- That is the whole coordination mechanism: there is no message passing
-- between steps, no shared object, no in-memory state. A step that dies
-- mid-run loses nothing that was not already durable.

-- ---------------------------------------------------------------------------
-- The queue
-- ---------------------------------------------------------------------------

-- One row per unit of work. `kind` names the step that will handle it, so a
-- step never needs to know which step comes after it: it enqueues a kind and
-- stops caring.
--
-- Retries live here rather than inside a step. A step that sleeps and tries
-- again holds a process open for the duration of the backoff and loses the
-- attempt count if it crashes. Persisting `attempts` and `next_attempt_at`
-- means the wait costs nothing and survives a restart.
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,            -- JSON
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 4,
    next_attempt_at TEXT NOT NULL,            -- ISO 8601 UTC
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_claimable ON tasks (status, next_attempt_at);

-- Where a task goes when it has run out of attempts, or failed in a way that
-- retrying cannot fix. Deliberately a table and not a log line: the point is
-- that `replay` can put a row back on the queue once the cause is fixed. A
-- failure you cannot reprocess without editing the database by hand is manual
-- work wearing an automation costume.
CREATE TABLE IF NOT EXISTS dead_letters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    attempts    INTEGER NOT NULL,
    error       TEXT NOT NULL,
    reason      TEXT NOT NULL,                -- 'exhausted' | 'permanent'
    created_at  TEXT NOT NULL,
    replayed_at TEXT
);

-- ---------------------------------------------------------------------------
-- Observability
-- ---------------------------------------------------------------------------

-- One row per task execution, appended, never updated. This is what makes
-- "did last night's run work" answerable with a query instead of by reading
-- container logs, and what `stats` aggregates.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,                -- ok | transient | permanent | invalid
    duration_ms INTEGER NOT NULL,
    error       TEXT,
    started_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at);

-- ---------------------------------------------------------------------------
-- Domain
-- ---------------------------------------------------------------------------

-- Applications as they arrived, before anything was decided about them.
-- `external_id` is the applicant id from the source form, and it is UNIQUE:
-- that single constraint is what makes ingestion idempotent. Replaying the
-- same batch twice is a no-op rather than a duplicate.
CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT NOT NULL UNIQUE,
    email       TEXT NOT NULL,
    handle      TEXT NOT NULL,
    platform    TEXT NOT NULL,
    followers   INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',  -- new | accepted | rejected | review
    reason      TEXT,
    raw         TEXT NOT NULL,                -- the payload exactly as received
    created_at  TEXT NOT NULL
);

-- An accepted applicant who has been onboarded. `tracking_code` is how their
-- posts are recognised later, and is unique per creator.
CREATE TABLE IF NOT EXISTS creators (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id       INTEGER NOT NULL,
    email               TEXT NOT NULL UNIQUE,
    handle              TEXT NOT NULL,
    platform            TEXT NOT NULL,
    tracking_code       TEXT NOT NULL UNIQUE,
    rate_cents_per_1k   INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    onboarded_at        TEXT NOT NULL
);

-- Posts found carrying a creator's tracking code.
--
-- `(platform, external_id)` is UNIQUE so that seeing the same post on ten
-- consecutive polls stores it once and only updates its view count. Without
-- that constraint, a payout would multiply by however many times the tracker
-- happened to run.
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id    INTEGER NOT NULL,
    platform      TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    url           TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    views         INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    UNIQUE (platform, external_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_creator ON posts (creator_id, published_at);

-- What a creator is owed for a period.
--
-- Amounts are integer cents. Never floats: 0.1 + 0.2 is not 0.3 in binary
-- floating point, and money that is off by a cent is a support ticket.
--
-- UNIQUE on (creator, period) plus a recompute-from-posts calculation means
-- running payout twice for the same period produces the same number rather
-- than paying twice. The amount is always derived from the posts table, never
-- accumulated into a running total that could drift.
CREATE TABLE IF NOT EXISTS payouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id   INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    post_count   INTEGER NOT NULL,
    total_views  INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    computed_at  TEXT NOT NULL,
    UNIQUE (creator_id, period_start, period_end)
);
