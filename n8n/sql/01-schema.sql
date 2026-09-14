-- The Postgres schema behind the n8n workflows.
--
--   psql "$DATABASE_URL" -f n8n/sql/01-schema.sql
--
-- Idempotent: every statement is IF NOT EXISTS, so re-running it is safe.

BEGIN;

-- ---------------------------------------------------------------------------
-- Domain
-- ---------------------------------------------------------------------------

-- Every applicant, including the rejected ones. UNIQUE external_id is what
-- makes re-reading the same feed store each applicant once.
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

-- UNIQUE (platform, external_id): the same post seen on ten polls is one row,
-- not ten payouts. The CHECK on views rejects nonsense numbers from a provider.
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

-- Integer cents, never float. UNIQUE (creator, period) means re-running a
-- month updates the row instead of paying twice.
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
-- Failures
-- ---------------------------------------------------------------------------

-- Anything that failed and needs a person: bad data, a provider that stayed
-- down after four retries, a payout held for review, or a crashed execution.
-- Set replayed_at once it has been dealt with.
CREATE TABLE IF NOT EXISTS dead_letters (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow_id         text        NOT NULL,
    workflow_name       text        NOT NULL,
    execution_id        text        NOT NULL,
    execution_url       text,
    node                text        NOT NULL,
    kind                text        NOT NULL,  -- poll|screen|onboard|track|payout|crashed
    payload             jsonb       NOT NULL,
    attempts            integer     NOT NULL DEFAULT 1,
    error               text        NOT NULL,
    reason              text        NOT NULL,  -- invalid|exhausted|permanent|crashed
    created_at          timestamptz NOT NULL DEFAULT now(),
    replayed_at         timestamptz,
    replay_execution_id text
);

CREATE INDEX IF NOT EXISTS idx_dead_letters_open
    ON dead_letters (created_at) WHERE replayed_at IS NULL;

-- One open 'invalid' row per applicant. The feed is re-read every six hours
-- and a broken record stays broken until fixed at the source; without this,
-- 2 bad applicants became 32 rows in a week.
CREATE UNIQUE INDEX IF NOT EXISTS idx_dead_letters_one_open_invalid
    ON dead_letters (kind, (COALESCE(payload->>'external_id', md5(payload::text))))
    WHERE reason = 'invalid' AND replayed_at IS NULL;

-- One row per crashed execution. n8n re-reports an interrupted execution on
-- every restart.
CREATE UNIQUE INDEX IF NOT EXISTS idx_dead_letters_one_per_crash
    ON dead_letters (workflow_id, execution_id)
    WHERE reason = 'crashed' AND execution_id <> 'none';

COMMIT;
