-- Operational configuration, as rows.
--
-- This started as a workaround. The instance runs with
-- N8N_BLOCK_ENV_ACCESS_IN_NODE=true, so $env resolves to nothing in Code
-- nodes and in expressions, and the alternative was to turn that off -- which
-- would let any Code node in any workflow on that instance read
-- DB_POSTGRESDB_PASSWORD and every other secret in the container, to save
-- reading four numbers.
--
-- It turned out to be the better home anyway, for a reason that has nothing
-- to do with n8n. A threshold in an environment variable has no history: you
-- cannot ask when it changed, who changed it, or what the accuracy was under
-- the old value. As a row it is data, `updated_at` answers the first
-- question, and eval_runs.config already records exactly which values each
-- score was measured under. The thing that decides who gets paid should be
-- auditable.
--
-- What is NOT here: the token guarding the replay webhook. That is an
-- n8n Header Auth credential, encrypted with the instance key, because it
-- authenticates a caller rather than configuring behaviour.
--
-- What IS here and deserves care: alert_webhook_url. Anyone who can read this
-- table can post to that channel. The table is therefore exactly as sensitive
-- as the database, which is an acceptable trade only because the role that
-- reads it is scoped to this one database.

BEGIN;

CREATE TABLE IF NOT EXISTS config (
    key         text        PRIMARY KEY,
    value       text        NOT NULL,
    note        text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Values are text and cast at the point of use. A typed column per setting
-- would need a migration for every new one; a jsonb blob would make "what
-- changed" a diff of one row instead of one field. Text plus an explicit
-- ::int is the version that stays boring.
INSERT INTO config (key, value, note) VALUES

('min_followers',    '5000',
 'Below this, decline. The lower of the two screening thresholds.'),
('review_followers', '20000',
 'Between the two, a human decides. At or above, accept automatically.'),

('rate_cents_per_1k', '250',
 'Integer cents per 1,000 views. Never a float: see the note in 01-schema.sql.'),
('min_payout_cents',  '100',
 'Below this, no payout. A sub-euro transfer costs more in fees than it moves.'),
('payout_sanity_multiplier', '3',
 'A period totalling more than this many times the previous one is held for a human.'),
('payout_ceiling_cents',     '500000',
 'Absolute ceiling. Above this the period is held regardless of what last month was.'),

('applications_url', 'https://example.invalid/applications',
 'Where the poller reads new applications from.'),
('platform_api_url', 'https://example.invalid/platform',
 'Base URL of the platform API. Its /posts endpoint must echo tracking_code.'),
('mailer_url',       'https://example.invalid/mail/send',
 'Transactional mail endpoint. Honours an idempotency_key.'),

('alert_webhook_url', '',
 'Slack, Discord, or another n8n webhook. Empty means alerts fail and are recorded, never silently skipped.'),
('editor_base_url',   'https://n8n.senaproject.online',
 'Used to build links into alerts. No trailing slash, or every link reads .../undefined.'),

('min_accuracy', '0.8',
 'Below this, the morning digest marks the screening rules as needing attention.'),

-- Generated here, on first insert, and never overwritten by a re-run: the
-- ON CONFLICT below leaves `value` alone. Two uuids is 244 random bits from
-- the server's own CSPRNG (gen_random_uuid is core since Postgres 13), which
-- is plenty for a token that only has to be unguessable by another website.
('dashboard_action_token',
 replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', ''),
 'Anti-CSRF token rendered into the dashboard''s forms. Basic auth alone lets any site the operator visits post to the action webhook.')

ON CONFLICT (key) DO UPDATE SET
    note = EXCLUDED.note,
    updated_at = now();
-- DO UPDATE deliberately does NOT touch `value`. Re-running this file after a
-- deploy refreshes the documentation and leaves production settings alone.
-- The opposite would quietly reset a tuned threshold back to the default, at
-- deploy time, which is the kind of bug that gets blamed on the rules.

COMMIT;
