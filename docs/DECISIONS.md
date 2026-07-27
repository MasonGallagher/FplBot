# Architecture decision records

Each entry states a decision, the context that forced it, and what it costs. The
ones worth reading first are **ADR-003** (a genuine conflict in the spec, resolved
against the spec's own instruction) and **ADR-009** (the biggest scope decision).

---

## ADR-001: AWS SAM, not CDK or Terraform

**Status:** accepted

**Context.** This is a scheduled Lambda with a table, a bucket and two schedules.
It needs local invocation for development and a build step that produces
Lambda-compatible binary wheels.

**Decision.** AWS SAM.

**Why.** `sam local invoke` and `sam build --use-container` are the two features
that actually matter here, and both are first-class in SAM. CDK would introduce a
synthesis step and a Node toolchain for a template that is essentially static.
Terraform is excellent but has no equivalent of `sam local invoke`, and its
Lambda packaging story requires more scaffolding than the whole application.

**Cost.** SAM's `Schedule` event type is the legacy Rules API, so we declare
`AWS::Scheduler::Schedule` resources by hand instead (see ADR-005). SAM templates
are also less expressive than CDK - acceptable at this size.

---

## ADR-002: Dependencies in a layer, not the function package

**Status:** accepted

**Context.** The function package is redeployed on every code change. The
dependencies change perhaps monthly.

**Decision.** All runtime dependencies live in `layer/requirements.txt` and ship
as a Lambda layer. The root `requirements.txt` is intentionally empty.

**Why.** The function artefact becomes a few dozen kilobytes of our own source,
so a code-only deploy takes seconds rather than minutes. It also gives a single
source of truth for what is installed, which is what the size check in
`buildspec-build.yml` measures.

**Cost.** One more resource, and a layer version to reason about during deploys.

---

## ADR-003: Devigging implemented in-house, not via `penaltyblog`

**Status:** accepted — **and it contradicts an explicit instruction in the spec**

**Context.** SPEC §4.6 says to use `penaltyblog` for devigging and *not* to
hand-roll it. SPEC §2 says not to add `scipy` (121 MB unzipped) or `pandas`, and
notes that Lambda's hard limit is 250 MB unzipped across function plus layers.

These conflict. `penaltyblog` depends on both scipy and pandas transitively, so
adding it means blowing the size budget.

**Decision.** Implement the two methods we actually need - Shin and power -
directly in `domain/devig.py`, roughly eighty lines between them, verified by
`tests/test_devig.py`.

**Why.** The 250 MB limit is a hard platform constraint; the library choice is
not. Both methods reduce to a monotonic scalar equation solved by bisection,
which needs no scipy and cannot diverge - a property worth having in something
running unattended.

**Cost.** We own two numerical methods we would otherwise have got for free. The
mitigation is that the tests pin the properties that matter (probabilities sum to
1 on a partition; longshots are shrunk relative to multiplicative), so a
regression is loud. If the size budget ever loosens - a container image
deployment, say - swapping back is a two-line change confined to
`implied_probabilities`.

**Flagged deliberately.** This is the one place the implementation knowingly
departs from a direct instruction in the spec, and it is documented here rather
than silently resolved.

---

## ADR-004: `extra="allow"` on every pydantic model

**Status:** accepted

**Context.** FPL adds fields mid-season without notice. `can_transact`,
`price_change_percent` and `scout_news_link` are all recent additions.

**Decision.** `extra="allow"` everywhere. Require only the fields whose absence
genuinely breaks scoring. Record every unknown field as a CloudWatch metric, once
per field per container.

**Why.** This runs against the usual instinct, which is to validate strictly. But
under `extra="forbid"` the first of those additions would have hard-failed every
parse and taken the bot off the air three hours before a deadline - because
someone at FPL added a harmless field.

Tolerating is not ignoring. `SchemaDriftDetected` is alarmed on, because a new
field is very often the visible edge of a semantic change to an existing one.

**Cost.** A genuinely malformed payload gets further into the system than it
would under strict validation. Mitigated by the business invariants (ADR-008),
which catch the semantic problems types cannot.

---

## ADR-005: `AWS::Scheduler::Schedule`, not SAM's `Schedule` event

**Status:** accepted

**Context.** The bot must fire at a consistent time relative to UK kick-offs, and
the UK changes clocks in late October and late March - both inside the season.

**Decision.** Declare `AWS::Scheduler::Schedule` resources explicitly.

**Why.** SAM's `Schedule` event type maps to the legacy EventBridge Rules API,
which is **UTC-only**. A UTC-only schedule silently shifts by an hour relative to
local kick-off times twice a season. EventBridge Scheduler supports
`ScheduleExpressionTimezone`, so `:07` past the hour stays `:07` past the hour in
London all year.

Scheduler also gives us a retry policy and a dead-letter queue per schedule,
which the Rules API does not.

**Cost.** More template verbiage: an explicit IAM role, an explicit target block,
and a `Lambda::Permission` equivalent expressed as a role policy.

---

## ADR-006: Integer epoch arithmetic for every deadline calculation

**Status:** accepted

**Context.** `events[]` carries both `deadline_time` (ISO-8601 string) and
`deadline_time_epoch` (Unix integer).

**Decision.** All deadline arithmetic uses `deadline_time_epoch`. The ISO string
is used for display only, never for maths.

**Why.** Comparing integers sidesteps timezones entirely, and therefore sidesteps
DST. The 48-hour window is `48 * 3600` seconds, not a date offset. There is no
correct-looking-but-wrong path available.

**Cost.** None. This is strictly simpler.

---

## ADR-007: Idempotency by DynamoDB conditional write, keyed on (gameweek, tier)

**Status:** accepted

**Context.** EventBridge Scheduler is at-least-once. Manual invocations happen.
Retries happen. A duplicate email erodes trust in every future one.

**Decision.** Before sending, `put_item` with
`ConditionExpression="attribute_not_exists(pk)"` on
`NOTIFY#{season}#{gw}#{tier}`. Take the lock *before* the expensive work; release
it if sending itself fails.

**Why.** DynamoDB evaluates the condition atomically as part of the write, so two
concurrent invocations cannot both succeed. That is a distributed lock in one API
call, with no lease to renew and nothing to clean up.

Keying on gameweek and tier - **never** on wall-clock time - is what makes it
work: a retry has a different timestamp but the same gameweek and tier.

**Cost.** We have chosen at-most-once per tier. If the process dies between
locking and sending, that tier's email is lost. That is the right trade: a missing
48h email is recoverable (24h and 3h still fire), a duplicate is not.

---

## ADR-008: Business invariants that report rather than raise

**Status:** accepted

**Context.** Pydantic can tell you a field is a string. It cannot tell you that
ownership across all players ought to sum to about 1500 because a squad has
fifteen slots.

**Decision.** A set of invariants checked after every bootstrap parse. They
**report** - metric, log, and an entry in the email's caveats - rather than
raising. The one exception is the UTC timezone assertion, which does raise.

**Why.** A failed invariant means "trust this less and tell the user", not
"abandon the run". Refusing to produce output because ownership summed to 1447
would be a worse outcome than producing it with a caveat.

The timezone assertion raises because there is no sensible degraded behaviour
when your clock assumptions are wrong - every deadline in the run would be
silently mistimed.

**Cost.** A violated invariant can still reach the user, as a caveat. That is the
intent.

---

## ADR-009: No squad state in v1

**Status:** accepted

**Context.** The obvious next feature is "tell me who to swap for whom", which
requires knowing the user's fifteen players, bank and free transfers.

**Decision.** The bot does not know the squad. It produces a ranked board of
candidates, not paired swaps, and it runs no ILP optimiser.

**Why.** Squad state requires either FPL authentication - explicitly out of scope,
and `users.premierleague.com` no longer resolves - or the user maintaining a
squad file, which is a synchronisation problem that goes stale silently and
produces confidently wrong advice.

A candidate board is also more useful than it sounds: the interesting question is
usually "who is worth owning this week", and the swap follows from that plus
knowledge only the user has.

**Cost.** The output requires the reader to do the last step themselves.
Mitigated by keeping `score_player -> Distribution` pure and ranking separate, so
a v2 optimiser consumes the same distributions unchanged.

---

## ADR-010: Monte Carlo, not analytic expectation

**Status:** accepted

**Context.** Expected points could be composed analytically from component
expectations. Most FPL models do exactly that.

**Decision.** Draw 4,000 samples per player, walking the causal chain: minutes
bucket -> minutes -> goals, assists, goals conceded, defensive actions, cards ->
points.

**Why.** Two reasons, and the first is decisive.

The rank-attacking objective needs the *shape* of the distribution, not its mean
(ADR-011). That shape is a mixture - the points distribution conditional on
"started" looks nothing like the one conditional on "came on for twenty minutes" -
and there is no tidy closed form for it.

Second, sampling gets the correlations right for free. Goals conceded and the
clean sheet are the same underlying event; drawing conceded goals once and
deriving the clean sheet from it makes them consistent by construction. Analytic
composition means remembering that dependency by hand, every time.

**Cost.** Sampling noise, and about 40 ms for 600 players. The RNG is seeded from
`(season, gameweek, tier)` so a run is reproducible - a difference between two
runs means the data changed, not the dice.

---

## ADR-011: Rank-attacking objective, not expected points

**Status:** accepted

**Context.** SPEC §1 states the objective as expected *rank* gain.

**Decision.** Rank on `xP - lambda*(ownership * xP) + mu*ceiling`, and report
mean, floor, ceiling and ownership for every pick.

**Why.** A 60%-owned player's points largely wash out against the field - his
value is defensive, in the sense that *not* owning him is what costs you. A
4%-owned player with a comparable ceiling accrues almost entirely to you. Ranking
on raw expected points cannot express that difference at all.

**Cost.** The board will sometimes rank a lower-xP player above a higher-xP one,
which looks wrong until you know why. The "why" line on each pick says so
explicitly, and this document exists partly to make the behaviour predictable.

`lambda` and `mu` are hand-picked priors and are exactly the coefficients SPEC
§5.5 says must be *fitted* against historical gameweeks using decision-level
metrics. They live in one `ModelTunables` object so that replacement is a
one-object change.

---

## ADR-012: Fuzzy matching requires a margin, not just a score

**Status:** accepted

**Context.** Fourteen `web_name` collision groups in the live data, including
`Wilson` x3 and `Phillips` x3.

**Decision.** A match is accepted only when the best score clears the threshold
**and** beats the runner-up by at least 6 points. Ties are rejected outright,
even at a score of 95.

**Why.** With three Wilsons in the pool, a 95 that ties another 95 carries no
information. Turning a coin flip into a confident recommendation is worse than
admitting we do not know - the recommendation would look entirely plausible.

The candidate pool is also built **by team first**, then position. Team cuts 558
players to about 28 and eliminates every cross-club surname collision in one
step; applying position first would leave ~140 midfielders and still contain
multiple Phillipses.

**Cost.** Some genuinely correct matches are refused. They appear in the email's
caveats as unmatched players, which is the honest outcome.

---

## ADR-013: The alias cache is keyed on the source's own id

**Status:** accepted

**Context.** Fuzzy matching is unavoidable for Understat and PremierInjuries,
neither of which exposes an FPL id.

**Decision.** Cache each resolution in DynamoDB against the *source's* stable
identifier - PremierInjuries' `data-id`, Understat's `id` - along with the method
and the score that produced it.

**Why.** Each player is then fuzzy-matched **once, ever**. That is what keeps the
blast radius of the fuzzy layer small: a bad match is made once and can be
corrected once, rather than being re-rolled every hour.

Storing the method and score is not bookkeeping. When a recommendation turns out
to be about the wrong player, the first question is "was this an exact join or an
89-point fuzzy match?", and you want to answer it without re-running anything.

**Cost.** A wrong cached match persists until corrected. Acceptable, because it
is *visible* and correctable, unlike a match that is silently re-derived.

---

## ADR-014: Verbatim bytes in the S3 archive

**Status:** accepted

**Context.** Every response could be archived either as received or as parsed.

**Decision.** Verbatim decoded body bytes, gzipped. Never a re-serialised parse.
Archived *before* parsing, so a parse failure still leaves the evidence behind.

**Why.** A re-serialised dump contains today's *interpretation* of the payload.
Fields our model dropped are gone; types it coerced are coerced. When the schema
drifts, the evidence you need has been destroyed by the very code you are trying
to debug.

**Cost.** Slightly more storage, and no query-friendly structure. Both irrelevant
at a couple of hundred megabytes a season, and a lifecycle rule moves it to
Glacier Instant Retrieval after 90 days.

---

## ADR-015: Two functions, not one

**Status:** accepted

**Context.** `element-summary/{id}/` is one HTTP request per player, and there
are 558 players.

**Decision.** A separate `BackfillFunction` with a 600-second timeout, on a
weekly schedule, against a bounded watchlist of about 120 players.

**Why.** With 1.5-second host spacing, the full player list would take fourteen
minutes - past the poll function's 120-second timeout. Inlining it would force us
either to raise every timeout or to drop the politeness policy.

It is also the one way to make this project expensive. The spec is explicit:
*gate it to once per gameweek.*

**Cost.** A second function, a second log group, a second schedule.

---

## ADR-016: Prod is gated by a human in the pipeline

**Status:** accepted

**Context.** This bot emails recommendations on a schedule tied to real
deadlines.

**Decision.** `Source -> Test -> Build -> Deploy(dev, dry-run) -> Smoke ->
Manual approval -> Deploy(prod)`.

**Why.** A bad deploy at T-3h on a Saturday is a wasted gameweek, and unlike most
systems there is no "roll forward next week" - the deadline has passed. Thirty
seconds of human attention is cheap insurance for a system whose failures are
timed.

The dev stage deploys with `DryRun=true` and schedules disabled, then the smoke
test invokes it for real. That exercises the whole path - live HTTP, real
DynamoDB writes, real rendering - and catches the class of failure unit tests
structurally cannot: a missing IAM permission, a wrong environment variable, a
layer that imports fine locally and not on arm64.

**Cost.** Deploys are not fully automatic. Intended.

---

## ADR-017: The test stage runs with no AWS credentials

**Status:** accepted

**Context.** SPEC §7: *tests must never hit live APIs.*

**Decision.** The CodeBuild test role has permission to write its own logs and
read the artefact bucket. Nothing else.

**Why.** Discipline is not a control. If a test grows a dependency on real
infrastructure, it fails in CI rather than passing quietly while burning
third-party quota. `respx` intercepts httpx at the transport layer, so no socket
is ever opened.

**Cost.** Integration coverage has to come from the post-deploy smoke test
instead, which is where it belongs anyway.
