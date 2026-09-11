-- The Postgres schema behind the n8n workflows.
--
-- This is schema.sql translated to Postgres, minus one table and plus three.
--
-- Gone: `tasks`. n8n *is* the queue -- its execution list, its per-node retry
-- settings and its wait states replace queue.py entirely, and keeping a
-- second queue alongside it would mean two things to reason about and two
-- places for work to get stuck.
--
-- Still here: `dead_letters`. n8n retries a node and then gives up, and what
-- it leaves behind is a failed execution, which is a log line with a good UI.
-- You cannot query it, you cannot see how much is waiting, and retrying it
-- re-runs the workflow from the trigger rather than the one item that failed.
-- So the dead letter stays a table.
--
-- New: golden_set, eval_runs, eval_cases. That is the accuracy layer, and
-- there is nothing in n8n to replace it with.
--
--   psql "$DATABASE_URL" -f n8n/sql/01-schema.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- Failure
-- ---------------------------------------------------------------------------

-- Where an item goes when n8n has stopped retrying it, or when it failed in a
-- way that retrying cannot fix.
--
-- workflow_id, execution_id and node are the three columns the Python version
-- does not have and this one cannot do without: with six workflows feeding
-- one table, "which automation broke" has to be a column rather than an
-- inference from the payload shape. execution_url is there so the alert links
-- to the failed run instead of describing it.
CREATE TABLE IF NOT EXISTS dead_letters (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow_id         text        NOT NULL,
    workflow_name       text        NOT NULL,
    execution_id        text        NOT NULL,
    execution_url       text,
    node                text        NOT NULL,
    kind                text        NOT NULL,  -- ingest|screen|onboard|track|payout|eval
    payload             jsonb       NOT NULL,  -- the item exactly as it failed
    attempts            integer     NOT NULL DEFAULT 1,
    error               text        NOT NULL,
    reason              text        NOT NULL,  -- exhausted|permanent|invalid|crashed
    created_at          timestamptz NOT NULL DEFAULT now(),
    replayed_at         timestamptz,
    replay_execution_id text
);

CREATE INDEX IF NOT EXISTS idx_dead_letters_open
    ON dead_letters (created_at) WHERE replayed_at IS NULL;

-- ---------------------------------------------------------------------------
-- Observability
-- ---------------------------------------------------------------------------

-- One row per step execution, appended, never updated.
--
-- n8n already stores executions, so this looks redundant until the first time
-- somebody asks "when did screening start failing". n8n's execution list is
-- per workflow, pruned by a retention setting, and not joinable against the
-- domain tables. This is one table you can GROUP BY.
CREATE TABLE IF NOT EXISTS runs (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow_id  text        NOT NULL,
    execution_id text        NOT NULL,
    kind         text        NOT NULL,
    status       text        NOT NULL,  -- ok|transient|permanent|invalid
    item_count   integer     NOT NULL DEFAULT 1,
    duration_ms  integer     NOT NULL DEFAULT 0,
    error        text,
    started_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_kind_status ON runs (kind, status, started_at DESC);

-- ---------------------------------------------------------------------------
-- Domain
-- ---------------------------------------------------------------------------

-- Applications as they arrived, before anything was decided about them.
-- UNIQUE external_id plus ON CONFLICT DO NOTHING is the whole of idempotency:
-- re-running a batch stores each applicant once.
--
-- Every applicant is stored, including the rejected ones. Keeping only the
-- accepted makes "how many applied and how many did we turn away"
-- unanswerable, and that is the first question anyone asks.
CREATE TABLE IF NOT EXISTS submissions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_id text        NOT NULL UNIQUE,
    email       text        NOT NULL,
    handle      text        NOT NULL,
    platform    text        NOT NULL,
    followers   integer     NOT NULL CHECK (followers >= 0),
    status      text        NOT NULL DEFAULT 'new'
                CHECK (status IN ('new', 'accepted', 'review', 'rejected')),
    reason      text,
    raw         jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_submissions_status
    ON submissions (status, created_at DESC);

CREATE TABLE IF NOT EXISTS creators (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    submission_id     bigint      NOT NULL REFERENCES submissions (id),
    email             text        NOT NULL UNIQUE,
    handle            text        NOT NULL,
    platform          text        NOT NULL,
    tracking_code     text        NOT NULL UNIQUE,
    rate_cents_per_1k integer     NOT NULL,
    status            text        NOT NULL DEFAULT 'active',
    onboarded_at      timestamptz NOT NULL DEFAULT now()
);

-- (platform, external_id) is UNIQUE so that seeing the same post on ten
-- consecutive polls stores it once and only updates the view count. Without
-- it a payout multiplies by however many times the tracker happened to run.
--
-- The CHECK on views is the data quality gate from models.py, moved into the
-- database. In Python it was a pydantic validator; here the workflow is not
-- the only thing that can write to this table, so the ceiling belongs where
-- nothing can route around it. It fires on data that is wrong, not on data
-- that is merely surprising.
CREATE TABLE IF NOT EXISTS posts (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    creator_id    bigint      NOT NULL REFERENCES creators (id),
    platform      text        NOT NULL,
    external_id   text        NOT NULL,
    url           text        NOT NULL,
    published_at  timestamptz NOT NULL,
    views         bigint      NOT NULL DEFAULT 0
                  CHECK (views >= 0 AND views <= 1000000000),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, external_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_creator ON posts (creator_id, published_at);

-- Amounts are integer cents. Never float, never numeric: money that is off by
-- a cent is a support ticket. UNIQUE (creator, period) plus a recompute from
-- posts means running payout twice for a period updates a row rather than
-- paying somebody twice.
CREATE TABLE IF NOT EXISTS payouts (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    creator_id   bigint      NOT NULL REFERENCES creators (id),
    period_start date        NOT NULL,
    period_end   date        NOT NULL,
    post_count   integer     NOT NULL,
    total_views  bigint      NOT NULL,
    amount_cents bigint      NOT NULL CHECK (amount_cents >= 0),
    computed_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (creator_id, period_start, period_end)
);

-- ---------------------------------------------------------------------------
-- The accuracy layer
-- ---------------------------------------------------------------------------

-- The labelled dataset the screening rules are measured against.
--
-- expected_decision is a human judgement, and it has to be: a golden set
-- generated from the rules scores 100% by construction and measures nothing
-- at all. Some rows here disagree with what the current thresholds produce.
-- Those disagreements are the entire value of the table.
CREATE TABLE IF NOT EXISTS golden_set (
    external_id       text PRIMARY KEY,
    email             text        NOT NULL,
    handle            text        NOT NULL,
    platform          text        NOT NULL,
    followers         integer,
    expected_decision text        NOT NULL
                      CHECK (expected_decision IN
                             ('accepted', 'review', 'rejected', 'invalid')),
    note              text        NOT NULL,
    labelled_by       text        NOT NULL DEFAULT 'jose',
    labelled_at       timestamptz NOT NULL DEFAULT now()
);

-- One row per evaluation. ruleset_version names the thing being scored, so
-- that changing a threshold and re-running produces a comparison rather than
-- overwriting the only number you had.
CREATE TABLE IF NOT EXISTS eval_runs (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ruleset_version text         NOT NULL,
    execution_id    text,
    total           integer      NOT NULL,
    correct         integer      NOT NULL,
    accuracy        numeric(5,4) NOT NULL,
    macro_f1        numeric(5,4) NOT NULL,
    per_class       jsonb        NOT NULL,  -- precision/recall/f1/support per decision
    config          jsonb        NOT NULL,  -- the thresholds this run used
    started_at      timestamptz  NOT NULL DEFAULT now()
);

-- One row per case per run. Without it you have the score and not the cases,
-- and "accuracy went from 0.88 to 0.93" is only actionable if you can list
-- the rows that moved.
CREATE TABLE IF NOT EXISTS eval_cases (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    eval_run_id bigint  NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    external_id text    NOT NULL,
    expected    text    NOT NULL,
    actual      text    NOT NULL,
    correct     boolean NOT NULL,
    note        text
);

CREATE INDEX IF NOT EXISTS idx_eval_cases_run ON eval_cases (eval_run_id, correct);

-- The confusion matrix as a view, because writing the pivot by hand every
-- time is how people stop looking at it.
CREATE OR REPLACE VIEW eval_confusion AS
SELECT eval_run_id, expected, actual, count(*) AS n
FROM eval_cases
GROUP BY eval_run_id, expected, actual;

-- The two mistakes that do not cost the same. Accepting somebody who should
-- have been rejected costs money; rejecting somebody who should have been
-- accepted costs a creator who never applies again. One accuracy number hides
-- both, which is why the digest reports them separately.
CREATE OR REPLACE VIEW eval_costly_errors AS
SELECT eval_run_id,
       count(*) FILTER (WHERE expected = 'rejected' AND actual = 'accepted')
           AS false_accepts,
       count(*) FILTER (WHERE expected = 'accepted' AND actual = 'rejected')
           AS false_rejects
FROM eval_cases
GROUP BY eval_run_id;

COMMIT;
