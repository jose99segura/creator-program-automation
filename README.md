# creator-program-automation

[![tests](https://github.com/jose99segura/creator-program-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/jose99segura/creator-program-automation/actions/workflows/ci.yml)

A reference implementation of the automation layer behind a creator program:
capture applicants, screen them, onboard them, track what they publish, and
work out what they are owed.

Written to be read. The domain is the least interesting part of it: swap
creators for affiliates, or posts for support tickets, and the machinery
underneath is the same. That machinery is the point, and it is four things:

- **Independent steps** that never call each other, over a durable queue
- **Retries that distinguish transient from permanent**, because retrying a
  request that is wrong does not make it right
- **A dead letter queue you can replay**, because a failure you cannot
  reprocess without hand-editing the database is manual work in disguise
- **Enough observability to know it is working without watching it**

```bash
git clone https://github.com/jose99segura/creator-program-automation
cd creator-program-automation
pip install -r requirements.txt
python -m creator_program demo
```

No database to install, no services to start. It runs on SQLite and canned
data, and the fake platform API fails a quarter of its calls on purpose, so
the retry path and the dead letter queue both execute during the demo. An
automation whose failure path has never run is an automation whose failure
path does not work.

---

## The pipeline

```
   applications (webhook, form, CSV)
              │
        ┌─────▼──────┐
        │  ingest    │  splits a batch into one task per applicant
        └─────┬──────┘
        ┌─────▼──────┐
        │  screen    │  validates, stores, decides: accept / review / reject
        └─────┬──────┘
        ┌─────▼──────┐
        │  onboard   │  creates the creator, derives a tracking code, emails
        └─────┬──────┘
        ┌─────▼──────┐
        │  track     │  finds posts carrying that code, upserts them
        └─────┬──────┘
        ┌─────▼──────┐
        │  payout    │  recomputes what is owed for a period
        └────────────┘

   any step that runs out of attempts ──► dead_letters ──► replay
```

Each step is a function `handle(conn, payload)`. It does one thing, it raises
rather than catching, and **it never imports another step**. The only way a
step can cause more work to happen is to enqueue a kind:

```python
queue.enqueue(conn, "onboard", {"external_id": submission.external_id})
```

That is what makes the boundaries real rather than aspirational. Reordering
the pipeline, running a step twice, or moving one to another machine are
configuration changes, not rewrites.

---

## The decisions worth arguing about

### A queue table, not a workflow engine

Airflow, Temporal and Prefect all solve this, and all of them hide exactly the
mechanics this repository exists to show. A table with `status`, `attempts`
and `next_attempt_at` also goes considerably further than people expect before
it becomes the bottleneck.

More importantly, it is behind four functions: `enqueue`, `claim`, `complete`,
`fail`. When the table stops being enough, that swap is one file.

### Retries live in the queue, not inside a step

A step that catches an error, sleeps and tries again holds a worker open for
the duration of the backoff, and loses its attempt count if the process dies.
Persisting `attempts` and `next_attempt_at` means the wait costs nothing and
survives a restart.

### Transient and permanent are different, and only the runner decides

```
TransientError   a timeout, a 503, a rate limit    → retry with backoff
PermanentError   a creator that does not exist     → dead letter immediately
ValidationError  a malformed payload               → dead letter, and tell somebody
anything else    probably a bug                    → treat as transient
```

Every step raises and none of them catch, so this classification exists in one
place. If each step decided for itself, they would drift, and the one that got
it wrong would be the one quietly hammering a dead endpoint at three in the
morning.

An unexpected exception is treated as transient on purpose: a genuine bug will
exhaust its attempts and land in the dead letter anyway, whereas calling every
unknown failure permanent throws away work that a fix and a replay could have
recovered.

### Idempotency by natural key, not by retry token

Every step can run twice without doing damage, and it is the schema that
guarantees it rather than the code remembering to check:

| Step | What makes it safe |
|---|---|
| `screen` | `UNIQUE(external_id)` plus `INSERT OR IGNORE` |
| `onboard` | tracking code is `sha1(external_id)`, so it is the same code every time |
| `track` | `UNIQUE(platform, external_id)` plus `ON CONFLICT DO UPDATE` |
| `payout` | `UNIQUE(creator, period)` plus a recompute from posts |

The tracking code is the one worth dwelling on. If it were random, a task that
crashed after creating the creator but before sending the email would, on
replay, either create a second creator or send a code that does not match the
stored one. A pure function of the input has neither problem.

### The order inside `onboard` is deliberate

Create the creator, then send the email. If the email fails, a replay re-runs
the whole step: the insert is a no-op and only the email is retried. Sending
first would risk welcoming somebody the database does not have.

### Money is integer cents, and always recomputed

`0.1 + 0.2` is not `0.3` in binary floating point, and a payout that is off by
a cent is a support ticket. Every amount is an `int` of cents end to end.

Amounts are derived from the posts table on every run, never accumulated into
a running total. A counter drifts the first time a task runs twice, and drift
is invisible until somebody complains.

### Validation is at the boundary, and a rejection is not a crash

Nothing reaches a table without passing a `pydantic` model. A bad applicant
caught there costs a log line; the same record caught in the payouts table
costs a transfer to the wrong person.

Note the one in `PostIn`: a post claiming a billion views is rejected. That is
a data quality gate rather than a business rule, because views feed the payout
calculation directly, so a provider returning a nonsense number turns straight
into a nonsense transfer.

Bad data is recorded as `invalid`, dead lettered without retrying, and
included in the alert. It is not an outage, and it is still something a human
needs to know about.

### The dead letter queue is a queue, not a graveyard

```bash
python -m creator_program dlq
python -m creator_program replay 4
```

Once the cause is fixed, the work is recoverable without anyone writing SQL by
hand. Replaying twice is refused, because otherwise a nervous operator pays
somebody three times.

### One alert per run

An outage at the platform API fails every creator being tracked. Thirty
creators must not become thirty messages: alert fatigue is how real failures
end up ignored. Failures accumulate through the drain and one message goes out
at the end, or none.

---

## Observability

Three layers, because they answer different questions.

**Structured logs**, one JSON object per line, fields as values rather than
prose:

```json
{"ts":"2026-09-09T19:31:33Z","level":"error","message":"task failed",
 "task_id":20,"kind":"track","status":"permanent","duration_ms":0,
 "error":"unknown tracking code 'CP-520EEABE'"}
```

```bash
python -m creator_program work | jq 'select(.status != "ok")'
```

**A `runs` table**, appended and never updated, so "when did this start
failing" is a query rather than an archaeology expedition through container
logs.

**A summary**, which is what you actually look at:

```
$ python -m creator_program stats

Last 24h by outcome
  ingest     ok            1  avg 0ms
  onboard    ok            4  avg 0ms
  onboard    permanent     1  avg 0ms
  screen     invalid       3  avg 2ms
  screen     ok            7  avg 2ms
  track      ok            3  avg 0ms
  track      permanent     1  avg 0ms
  track      transient     2  avg 0ms

Dead letters awaiting a decision: 5
```

That is the whole health picture in eight lines: what ran, how it ended, how
long it took, and how much is waiting for a human.

---

## Scaling to 10x, and to 100x

The honest version, which is that most of it does not need to change.

**Today.** One process, one SQLite file, a scheduled drain. Thousands of
creators is not a lot of rows.

**10x: more workers.** Move to Postgres and change one line in `queue.claim`:

```sql
SELECT * FROM tasks
WHERE status = 'pending' AND next_attempt_at <= now()
ORDER BY next_attempt_at, id LIMIT 1
FOR UPDATE SKIP LOCKED          -- <-- this
```

`SKIP LOCKED` lets N workers drain the same table without ever handing the
same task to two of them. **No step changes**, because no step knows who is
executing it. This is the payoff for the steps not calling each other.

**10x: the tracker is what gets expensive.** `track` runs per creator per
poll, so it grows linearly with the roster while everything else grows with
events. Two changes, in this order:

1. Poll by cursor instead of re-fetching every creator's full history
2. Shard by `creator_id % N` so each worker owns a slice and cache locality
   stops being accidental

**100x: rate limits become the constraint, not compute.** A token bucket per
external API, shared across workers through Redis. At that point the retry
policy matters more than the parallelism: without jitter, every task that
failed during an outage retries in the same instant and knocks the provider
straight back over. It is already jittered.

**When the table finally gives up**, replace `queue.py` with SQS, Redis
Streams or Temporal. It is one file, four functions, and the steps do not
change. That is the entire reason the queue was isolated in the first place.

**What would need real thought**, and is deliberately not solved here: exactly
once payouts across a distributed worker pool. The `UNIQUE(creator, period)`
constraint plus a recompute makes it idempotent, which is enough for one
database. Money leaving the system through a real payment provider would need
an outbox table and a provider-side idempotency key, and that is a different
piece of work rather than a bigger version of this one.

---

## Commands

```bash
python -m creator_program init                 # create the tables
python -m creator_program ingest               # queue a batch of applications
python -m creator_program work                 # process everything that is due
python -m creator_program payout --start 2026-01-01 --end 2027-01-01
python -m creator_program payouts              # what has been computed
python -m creator_program stats                # queue depth, outcomes, totals
python -m creator_program dlq                  # tasks that gave up
python -m creator_program replay 4             # put one back on the queue
python -m creator_program demo                 # all of the above, end to end
```

## Layout

```
schema.sql                 the tables, plain SQL, no ORM
creator_program/
  config.py                thresholds, rates, retry policy, all in one place
  db.py                    connection and schema
  queue.py                 enqueue / claim / complete / fail / replay
  retry.py                 TransientError, PermanentError, backoff
  models.py                the validation gate
  obs.py                   JSON logs, run records, counters, alerting
  runner.py                the worker loop and failure classification
  steps/                   ingest, screen, onboard, track, payout
  providers/               a fake platform API and mailer, which fail on purpose
tests/                     payouts, the retry path, validation, the n8n graphs
n8n/                       the same pipeline as seven n8n workflows
  README.md                what each one proves, and how to set them up
  sql/                     the Postgres schema and the labelled dataset
  workflows/               00 rules, 01 pipeline, 02 payout, 03 errors,
                           04 replay, 05 eval, 06 digest
```

## Tests

```bash
python -m pytest -q
```

77 tests, weighted towards the two places where a bug is expensive: the payout
arithmetic and the failure path. The failure path is the half of any
automation that only runs when things break, and therefore the half most
likely to be broken without anyone noticing.

## The n8n version

[`n8n/`](n8n/README.md) is the same pipeline built as seven n8n workflows, for
the case where the team already runs n8n and a Python service would be a new
thing to operate. The mapping of the machinery is direct:

| Python | n8n |
|---|---|
| `retry.py` | `retryOnFail`, `maxTries`, `waitBetweenTries` on the HTTP nodes |
| `PermanentError` | the error output of a node, routed away from the retry path |
| `queue.py` | n8n itself: executions, retries and wait states |
| `dead_letters` | still a table -- n8n's retry re-runs the whole trigger, not one item |
| `runner.py` catching everything | an Error Trigger workflow set on every workflow |
| one alert per run | `executeOnce`, plus a fifteen minute suppression window |
| `INSERT OR IGNORE` | `ON CONFLICT DO NOTHING` in the same statements |

Two things it has that the Python side does not, because n8n forced the
question:

**The business rules are their own workflow.** There is no import statement in
n8n, so shared logic is either a workflow boundary or it is copy-paste. `00
screening rules` is called by the pipeline and by the eval, which is the only
reason the eval measures the code production runs rather than a copy of it.

**The decision has a measured accuracy.** `05 eval screening` scores the rules
against 46 hand-labelled applications and writes accuracy, macro F1, per-class
precision and recall, and the confusion matrix to a table on every run. Seven
of those labels disagree with the current thresholds on purpose: a golden set
generated from the rules scores 1.0 for ever and measures nothing.

The graphs are structurally validated in CI -- connections, reachability,
unconnected error outputs, and the fact that the ruleset appears in exactly
one file. They have **not** been run against a live n8n instance. Treat that
directory as a reviewed design; the Python side is the tested one.

## Configuration

Everything is optional; see `.env.example`. The two worth knowing about:

```bash
PLATFORM_FAILURE_RATE=0.25   # set to 0 for a clean run, raise it to fill the DLQ
ALERT_WEBHOOK_URL=           # Slack, Discord, an n8n webhook. Unset is quieter, never silent
```
