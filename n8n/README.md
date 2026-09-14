# The n8n version

The same pipeline as the Python code one directory up, as **three n8n
workflows**, one Postgres database, and a page to watch it.

| Workflow | When it runs | What it does |
|---|---|---|
| `01 pipeline` | every 6 hours, by hand, or from the dashboard | reads new applications, screens them, onboards the accepted ones, fetches their posts |
| `02 payout` | 1st of the month, 04:00 | works out what each creator is owed for last month |
| `03 errors` | whenever another workflow crashes | records the failure and sends an alert |
| `04 dashboard` | when you open it | the page below; it does no work of its own |
| `99 fake providers (demo)` | when called | stand-ins for the application form, platform API, mailer and alert channel, so the demo runs with no external accounts. The platform endpoint fails a quarter of its calls on purpose, so retries and dead letters really happen |

In production the three URLs in `config` point at `99`. To go live, point
them at real services and deactivate `99`.

## The dashboard

`https://n8n.senaproject.online/webhook/creator-program/dashboard`, behind
basic auth (credential `creator dashboard login`). It is in Spanish and reads
live data:

- **Cómo funciona**: the three workflows step by step, each step with a real
  example from the database.
- **Las reglas**: the follower thresholds as a scale, with the latest real
  applicants in each outcome, plus the invalid ones and why.
- **Un creador, de principio a fin**: the creator with the most views, from
  application to payout, with the payout arithmetic redone on the page.
- **Pagos por mes**, **Fallos pendientes** (each with what to do, and a
  *Marcar resuelto* button) and **Creadores**.

It has two buttons: *Lanzar el pipeline ahora* and *Marcar resuelto*. Both
check a token that only the rendered page has, so another site cannot trigger
them using your saved login.

To change it, edit `dashboard/page.html` (open it straight in a browser to
preview with made-up data), then run `python n8n/dashboard/build.py` to embed
it into `04-dashboard.json`. CI fails if you forget to rebuild.

```
  every 6h ─► 01 pipeline ─► submissions, creators, posts ─► 02 payout (monthly) ─► payouts
                   │                                              │
                   └───────── failures ─► dead_letters ◄──────────┘
                                               ▲
                     anything that crashes ─► 03 errors ─► alert
```

---

## 01 pipeline, step by step

```
Config ─► Fetch applications ─► Screen applicants ─► Valid? ─► Store applicant ─► Accepted?
                                                       │                             │
                                                       no                           yes
                                                       ▼                             ▼
                                                  Dead letter    Create creator ─► Send welcome email
                                                                                     ─► Fetch posts ─► Upsert posts
```

- **Screen applicants** holds all the rules: the platform must be YouTube,
  TikTok or Instagram, and follower count decides the outcome — under
  `min_followers` is rejected, under `review_followers` goes to review, anything
  higher is accepted.
- **Review and rejected** applicants are stored and stop there. A person moves
  them to `accepted` by hand if they want to, and the next run picks that up.
- **The HTTP calls** retry 4 times, 5 s apart. If they still fail, the item goes
  to `dead_letters` and **one** alert is sent per run.
- **Running it twice is safe.** Applicants, creators and posts all have unique
  keys, and the tracking code is derived from the application id, so it comes
  out the same every time.

## 02 payout

It recomputes `views × rate` from the posts table in integer cents, so running
it again for the same month updates the rows instead of paying twice. Two
things put the month on hold (a row in `dead_letters` plus an alert) instead of
reporting it:

- the total is above `payout_ceiling_cents`
- the total is more than `payout_sanity_multiplier` × last month

Nothing is ever transferred by this workflow.

## 03 errors

It is the Error Workflow for 01 and 02, and catches anything their own error
branches did not. It writes a `crashed` row to `dead_letters`, then alerts. If
n8n reports the same crashed execution again after a restart, the second report
is ignored.

---

## Settings

Everything tunable is a row in the `config` table (`sql/02-config.sql`),
because `$env` is blocked on this n8n instance:

```sql
SELECT key, value, note FROM config;
UPDATE config SET value = '8000', updated_at = now() WHERE key = 'min_followers';
```

The only credential is the Postgres one, `creator_prod postgres` (id
`creatorPgCred001`).

## Dealing with failures

```sql
SELECT id, kind, reason, error, execution_url
FROM dead_letters WHERE replayed_at IS NULL ORDER BY created_at DESC;
```

| reason | means | do |
|---|---|---|
| `invalid` | the applicant's data is wrong | fix it at the source; the next run picks it up |
| `exhausted` | a provider stayed down | run `01 pipeline` again once it is back |
| `permanent` | a payout month was held | check the numbers, then run `02 payout` again |
| `crashed` | a node threw | open `execution_url` |

When a row is dealt with, mark it: `UPDATE dead_letters SET replayed_at = now() WHERE id = …;`

## Deploy

```bash
scp -r -i ~/.ssh/coolify_vps n8n ubuntu@51.195.223.171:/tmp/creator-n8n
ssh -i ~/.ssh/coolify_vps ubuntu@51.195.223.171
CREATOR_DB_PASSWORD="$(cat ~/.creator_prod_password)" sudo -E bash /tmp/creator-n8n/deploy.sh
```

This applies the schema and config as the `creator_prod` role, imports the
three workflows and activates them. Every workflow has a fixed id, so a
re-import updates it rather than making a copy.

Before activating, point `applications_url`, `platform_api_url` and
`mailer_url` at real endpoints. Otherwise every run just fills `dead_letters`.

## Lessons from the first real runs

Every one of these passed structural checks and failed silently:

- `sum()` over `bigint` returns `numeric` in Postgres, so every payout came out a
  cent high until the query cast it back with `::bigint`.
- `$env` is undefined on this instance, with no error at all. That is why
  settings live in the `config` table.
- `splitOut` with *include other fields* nests the data under `data` instead of
  spreading it, so a Code node is used instead.
- `$execution.startedAt` is undefined inside sub-workflows. That is one reason
  this version has none.

## Tests

`tests/test_n8n_workflows.py` checks the JSON structure: every connection
points at a real node, every node is reachable, error outputs are wired,
there are no secrets and no `$env`. It cannot prove the queries run. Only n8n
can.
