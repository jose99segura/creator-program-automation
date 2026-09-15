-- The demo's fake applicants, now a table instead of a hardcoded array in
-- the "99 fake providers" workflow. Add, edit or remove one from
-- https://n8n.senaproject.online/webhook/creator-program/fake/admin instead
-- of editing the workflow's code.
--
--   psql "$DATABASE_URL" -f n8n/sql/04-fake-applicants.sql
--
-- Idempotent: the CREATE is IF NOT EXISTS and the seed only inserts rows that
-- are not already there, so re-running this never resets what someone edited
-- on the admin page.

BEGIN;

CREATE TABLE IF NOT EXISTS fake_applicants (
    external_id text        PRIMARY KEY,
    email       text        NOT NULL,
    handle      text        NOT NULL,
    platform    text        NOT NULL,
    followers   integer     NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- The original eight, chosen so that one poll exercises every screening
-- outcome: 3 accepted, 3 review, 2 rejected. None are malformed on purpose --
-- they would be dead lettered again on every poll and keep the dashboard's
-- pending failures from ever clearing. Add a malformed one yourself from the
-- admin page to see the 'invalid' path.
INSERT INTO fake_applicants (external_id, email, handle, platform, followers) VALUES
('demo-01', 'ana@example.com',  'anacooks',   'youtube',     142000),
('demo-02', 'ben@example.com',  '@benbuilds', 'TikTok',      88000),
('demo-03', 'cara@example.com', 'caradraws',  ' instagram ', 51000),
('demo-04', 'jack@example.com', 'jackjams',   'tiktok',      12000),
('demo-05', 'kim@example.com',  'kimknits',   'instagram',   8200),
('demo-06', 'nia@example.com',  'niaknows',   'youtube',     15750),
('demo-07', 'quin@example.com', 'quinquilts', 'tiktok',      1200),
('demo-08', 'rosa@example.com', 'rosaruns',   'instagram',   84)
ON CONFLICT (external_id) DO NOTHING;

COMMIT;
