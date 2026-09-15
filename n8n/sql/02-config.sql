-- Settings, as rows. The workflows read this table instead of $env, which is
-- blocked on the instance.
--
-- Change a value with:
--   UPDATE config SET value = '8000', updated_at = now() WHERE key = 'min_followers';
--
-- Re-running this file refreshes the notes but never overwrites a value.

BEGIN;

CREATE TABLE IF NOT EXISTS config (
    key         text        PRIMARY KEY,
    value       text        NOT NULL,
    note        text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO config (key, value, note) VALUES

('min_followers',    '5000',
 'Below this, reject.'),
('review_followers', '20000',
 'Between the two thresholds a human decides. At or above, accept.'),

('rate_cents_per_1k', '250',
 'Integer cents per 1,000 views.'),
('min_payout_cents',  '100',
 'Below this, no payout.'),
('payout_sanity_multiplier', '3',
 'A month totalling more than this many times the previous one is held for review.'),
('payout_ceiling_cents',     '500000',
 'A month totalling more than this is held for review regardless.'),

('applications_url', 'https://example.invalid/applications',
 'Where the pipeline reads new applications from. Must return { data: [...] }.'),
('platform_api_url', 'https://example.invalid/platform',
 'Platform API base URL. GET /posts?tracking_code= must return { tracking_code, data: [...] }.'),
('mailer_url',       'https://example.invalid/mail/send',
 'Transactional mail endpoint.'),

('alert_webhook_url', '',
 'Slack, Discord, or any webhook taking { text }.'),
('editor_base_url',   'https://n8n.senaproject.online',
 'Used to link alerts to executions. No trailing slash.'),

('prep_model', 'claude-opus-5',
 'Model for the interview prep simulator. Must support structured outputs and server-side fallbacks.'),

-- Generated once on first insert; the ON CONFLICT below never overwrites it.
('dashboard_action_token',
 replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', ''),
 'Anti-CSRF token for the dashboard buttons. Basic auth alone lets any site post to the action webhook.')

ON CONFLICT (key) DO UPDATE SET note = EXCLUDED.note;

COMMIT;
