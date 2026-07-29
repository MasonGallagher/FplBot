# Architecture

How the pieces fit, and why each one is there. If you want the *decisions* rather
than the shape, read [DECISIONS.md](DECISIONS.md). For the same shape as rendered
diagrams — including a plain-English version for non-technical readers — see
[DIAGRAMS.md](DIAGRAMS.md).

---

## 1. The shape of a run

Every hour, at seven minutes past, EventBridge Scheduler invokes the poll
function. What happens next:

```
  EventBridge Scheduler  (cron(7 * * * ? *), Europe/London)
            |
            v
  +---------------------------------------------------------------+
  |  PollFunction  (arm64, 1024 MB, 120s, reserved concurrency 1)  |
  +---------------------------------------------------------------+
            |
      1.    +--> FPL bootstrap-static  --> assert timezone, check invariants
            |
      2.    +--> SNAPSHOT to DynamoDB          <-- ALWAYS, every run
            |
      3.    +--> next_deadline()
            |      no deadline? -> exit 0 (off-season / season over)
            |
      4.    +--> due_tier()
            |      no tier crossed? -> exit 0 (snapshot only)
            |
      5.    +--> acquire_notification_lock()   <-- conditional write
            |      already held? -> exit 0 (suppressed)
            |
      6.    +--> ingest third-party sources
            |      ClubElo -> FFS lineups -> PremierInjuries -> Understat -> Odds
            |      each behind a circuit breaker, each with a fallback
            |
      7.    +--> staleness gate (24h ceiling)
            |      too stale? -> email about the FAILURE, not about transfers
            |
      8.    +--> resolve identities (photo code -> cache -> exact -> fuzzy)
      9.    +--> transfer-flow analysis -> availability signals
     10.    +--> score every player (Monte Carlo) -> Distribution
     11.    +--> project the season (neutral fixture x fixture load)
     12.    +--> rank: buy board, captains, wildcard squad -> Board
     13.    +--> benchmark vs ep_next; STORE PREDICTIONS for later grading
     14.    +--> render HTML + text
     15.    +--> archive to S3, send via SES
```

Steps 1-5 are cheap and always run. Step 6 onwards is the expensive half, and it
only happens when there is actually an email to send. That ordering is why the
bot costs pennies: 750 invocations a month, of which perhaps twenty do real work.

---

## 2. Layering

Dependency arrows point downwards only. Nothing below reaches up.

```
  handlers/     AWS entry points. Parse the event, call pipeline, shape a response.
      |
      v
  pipeline.py   The only module that knows the whole story.
      |
      +-------------------+-------------------+
      v                   v                   v
  report/             sources/             domain/
  render + SES        ALL network I/O      PURE LOGIC
                                           deadline, fixtures, identity,
                                           scoring, ranking, captaincy,
                                           horizon, squad
      |                   |                   ^
      |                   v                   |
      |               http/  storage/         |
      |                   |                   |
      +-------------------+-------------------+
                          v
                      models/
```

### Why `domain/` is the important boundary

Everything in `domain/` is a pure function of its arguments. No I/O, no AWS, no
HTTP, and no clock reads - `now_epoch` is always passed in.

That constraint is not aesthetic. It means:

- **Every interesting bug is testable without mocking.** A blank gameweek
  misclassified, a fuzzy match on the wrong Wilson, a z-score computed from three
  observations - all of these are plain function calls with plain arguments.
- **Tests can place themselves anywhere in the season.** Deadline logic is tested
  in pre-season, in the post-deadline window, and after the final gameweek, by
  passing three different integers.
- **Scoring is reproducible.** `score_player` given the same inputs returns the
  same distribution, which is what lets a future squad optimiser consume it.

Roughly 85% of the test suite exercises `domain/` and touches nothing else.

### Why all I/O is in `sources/`

One rule: **no source module calls httpx directly.** Everything goes through
`fplbot.http.HttpClient`.

That is what makes the retry policy, the rate limiting, the redirect ban, the
content-type assertion and the circuit breaker uniform - and, crucially,
impossible for a module written six months from now to forget.

---

## 3. AWS resources, and why each

### Lambda, arm64, two functions

**arm64 (Graviton2)** is about 20% cheaper per GB-second than x86_64 and
measurably faster for numpy. Every dependency publishes an aarch64 manylinux
wheel, so there is no compatibility cost.

**Reserved concurrency 1** on both functions. This is a scheduled singleton;
concurrent runs would duplicate third-party requests and race on the snapshot.
The idempotency lock would still hold - it is a conditional write - but there is
no reason to allow the race at all.

**Two functions, not one**, because `element-summary` is one HTTP request *per
player*. With 1.5-second host spacing, 558 players is fourteen minutes - well past
the poll function's 120-second timeout. The backfill gets its own function with a
600-second timeout, its own weekly schedule, and a bounded watchlist.

### DynamoDB, single table, on-demand

One table, six item types, distinguished by the `pk` prefix:

| Purpose | pk | sk |
|---|---|---|
| Bootstrap snapshot (gzipped) | `SNAP#{season}` | `{iso8601}` |
| Per-player series | `PLAYER#{season}#{element}` | `{iso8601}` |
| Notification lock | `NOTIFY#{season}#{gw}#{tier}` | `LOCK` |
| Stored predictions (gzipped) | `PRED#{season}#{gw}` | `{tier}` |
| Last-known-good pointer | `LKG#{season}` | `{source}` |
| Resolved id alias | `ALIAS#{source}` | `{source_id}` |

Predictions are the one item type keyed on neither a timestamp nor a source, and
for the same reason as the notification lock: the question asked of them later is
always "what did we say about GW7?", never "what did we say at 14:07". The sort
key is the tier, so the T-24h and T-3h boards are graded separately - which is
the entire reason to keep both.

Sort keys are ISO-8601 timestamps, which gives range queries for free: ISO-8601
sorts lexicographically in the same order it sorts chronologically. That single
property is the entire reason to prefer it over any other format here.

**On-demand billing** because traffic is a handful of writes per hour with no
sustained baseline. Provisioned capacity would cost more and demand capacity
planning for a workload that does not vary.

**TTL is 400 days on snapshots, not 30.** This series is the only training set
for the price model and the only source of intra-gameweek transfer velocity.
Losing it to a short TTL would be silent and irreversible, and keeping it costs
pennies.

**Retained on stack delete in production.** An accidental `delete-stack` must not
be able to destroy a season of data that cannot be re-collected.

### S3, raw archive

Every upstream response, gzipped, **verbatim bytes**.

The reason is schema drift. When FPL changes a field's meaning next February, the
question you need to answer is "what exactly did the payload look like before and
after?". An archive of `orjson.dumps(parsed_model)` contains today's
*interpretation* - dropped fields are gone, coerced types are coerced - and has
destroyed the evidence you came for.

Verbatim bytes also make backtests honest: a replay from raw bytes exercises the
parser, a replay from a parsed dump does not.

Layout is date-partitioned (`raw/{source}/{yyyy}/{mm}/{dd}/...`) because that is
how you will query it, and because a lifecycle rule to Glacier Instant Retrieval
after 90 days is then a one-line prefix rule.

### EventBridge Scheduler, not the legacy Rules API

`AWS::Scheduler::Schedule`, not SAM's `Schedule` event type. The latter maps to
the legacy Events Rules API, which is **UTC-only** - and a UTC-only schedule
silently shifts by an hour relative to UK kick-off times when the clocks change.
Scheduler supports a real timezone, so `:07` past the hour stays `:07` past the
hour in London all year.

`:07` rather than `:00` for two reasons. The top of the hour is when every
scheduled job in the world fires. And FPL sits behind Fastly with a 300-second
edge TTL, so a request at exactly `:00` may return a response assembled before
the minute rolled over.

### SES, not SNS

SNS email is plain text only and prefixes every message with subscription
boilerplate. For a report built from tables, that is not a cosmetic difference.

---

## 4. Failure handling

The policy, from the spec: **always produce output.** A recommendation from
imperfect data, clearly flagged, beats an apologetic silence three hours before a
deadline.

Three mechanisms implement it.

### Circuit breakers, per source

After three consecutive failures a source is cut off for the rest of the
invocation. This converts a slow repeated failure - three retries times a
20-second timeout, eating a 120-second budget - into a fast single one.

State is per-process, so it survives a warm container. If Understat blocked our
IP an hour ago it is probably still blocking it now; a cold start gets a clean
slate and one honest attempt, which is also correct.

### Last-known-good, per source

On failure, `with_fallback` loads the last successful parse from DynamoDB, marks
the source **degraded**, and adds a caveat naming the age of the data.

Not every source gets this. Predicted line-ups deliberately do **not**: a previous
gameweek's XI is not stale data, it is *wrong* data - it would confidently assert
that a rotated-out player is starting. Better to lose the feature and say so.

### The staleness ceiling

Above 24 hours we refuse. The email then describes the *failure* rather than
recommending transfers, because silence would be read as "the bot ran and found
nothing worth saying".

---

## 5. Observability

Three Powertools facilities, and one alarm that matters more than the others.

**Structured JSON logs** with the request id and cold-start flag on every line,
so CloudWatch Logs Insights can query them.

**EMF metrics** - emitted as specially-shaped log lines, turned into CloudWatch
metrics for free. No `PutMetricData` call, no latency, no per-metric cost.

**X-Ray tracing** around each source fetch, so a slow run is diagnosable without
scattering timing logs.

### Calibration, the one measurement that is not about uptime

Everything above watches whether the bot *ran*. The weekly calibration report in
`domain/calibration.py` is the only thing that watches whether it was *right*:
the backfill grades the settled gameweek's stored predictions against the points
actually scored, and logs RMSE, Spearman, a Brier score on P(haul) and P10-P90
coverage.

Coverage is the one to read. `mu * ceiling` is a term in the ranking objective,
so an overstated P90 reorders the board while the mean stays perfectly
calibrated - a failure mode no alarm here would ever fire on. See MODEL.md
section 8.

Deliberately logged rather than emitted as EMF. Unlike the metrics above, which
are free because they ride on log lines already being written, a *custom* metric
costs per metric per month, and the stack already publishes 14 against a free
tier of 10. These fire once a week and are read in Logs Insights when somebody is
asking the question.

### `SchemaDriftDetected` is the important one

From the spec: *silent schema drift is a bigger risk than an outright outage,
because it yields confident, wrong recommendations.*

An outage is obvious - no email arrives. A payload where `selected_by_percent`
has quietly become a fraction still parses, still renders a beautiful email, and
has inverted every rank-attacking calculation in it.

So models use `extra="allow"` (a new FPL field must never take the bot off the
air) but every unknown field is recorded as a metric, once per field per
container. A new field is very often the visible edge of a semantic change to an
existing one, and knowing a week early is the difference between noticing and
being quietly wrong.

`InvariantViolated` covers what types cannot: total ownership summing to about
1500%, exactly twenty teams, thirty-eight events, empty `overrides.rules`.

### `PollSilenceAlarm`

A silent bot looks identical to a working bot. This alarm fires when the poll
function has not been invoked for three hours - arguably the failure most likely
to go unnoticed, because nothing is broken and no error is logged.

---

## 6. The v2 seam

`score_player(element, team_gameweek, minutes, availability, context)` returns a
`Distribution` and is pure. Ranking is a separate module that consumes those
distributions.

A future squad-aware ILP optimiser - over `{buy, sell, hold}` with budget,
three-per-club, formation and free-transfer constraints - consumes exactly the
same distributions, unchanged. That is the whole point of the split, and it is
why `ranking.py` never reaches back into `scoring.py`.

It is deliberately **not** built. See
[ADR-009](DECISIONS.md#adr-009-no-squad-state-in-v1).
