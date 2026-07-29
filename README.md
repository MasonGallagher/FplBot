# fplBot

A scheduled AWS Lambda that watches for Fantasy Premier League gameweek
deadlines, ingests official and third-party football data, scores every player,
and emails a ranked board of who to transfer **in** and who to transfer
**out / avoid**.

Built from [`SPEC.md`](SPEC.md), which is the output of research that probed the
live APIs rather than trusting documentation. Several widely-repeated facts about
the FPL ecosystem turn out to be wrong; where this codebase looks surprising, the
surprise is usually deliberate and there is a comment explaining it.

---

## Contents

| Document | What it covers |
|---|---|
| This file | Getting it running, and how the pieces fit |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layering, data flow, why each AWS service |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Architecture decision records - the *why* behind each choice |
| [docs/MODEL.md](docs/MODEL.md) | How a player becomes a number, and why it is a distribution |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Operating it: alarms, failure modes, what to do at 3am |
| [docs/LEGAL.md](docs/LEGAL.md) | Terms of service position for each data source |
| [SPEC.md](SPEC.md) | The original build specification |

---

## Quick start

```bash
git clone https://github.com/MasonGallagher/fplBot.git
cd fplBot
./deploy.sh
```

On a first run `deploy.sh` will offer to create a `.env` for you, run the test
suite, verify your SES email identities, build the Lambda package and deploy the
stack. It is idempotent - run it again after any change.

**Prerequisites**

| Tool | Why |
|---|---|
| AWS CLI, configured | Everything |
| AWS SAM CLI | Building and deploying |
| Docker *(recommended)* | Builds dependencies against the real Lambda runtime |
| Python 3.13 *(3.12 works for tests)* | Running the suite locally |

Docker is not strictly required, but without it `deploy.sh` builds natively and
warns you. The risk is real: `numpy`, `orjson`, `selectolax` and `rapidfuzz` all
ship compiled code, and a wheel built against the wrong architecture or libc
imports perfectly on your machine and fails inside Lambda with an error naming a
`.so` file and nothing else.

### Useful invocations

```bash
./deploy.sh --test-only              # just run the tests
./deploy.sh --dry-run                # deploy; render reports but never send
./deploy.sh --env prod               # production, with live schedules
./deploy.sh --invoke                 # deploy, then run once and show the logs
./deploy.sh --pipeline               # deploy the CI/CD pipeline instead
./deploy.sh --delete --env dev       # tear a stack down
```

### Before the first real email

1. **Confirm the SES verification links.** AWS emails one per address. Nothing
   sends until they are clicked, and they often land in spam.
2. **Deploy once with `--dry-run`** and read the rendered report in CloudWatch
   Logs. It costs nothing and catches a broken template before it reaches you.
3. **Confirm the alarm SNS subscription**, also emailed by AWS.

---

## What it actually does

### Two phases, and why the second one is the important one

The bot polls **hourly**, at seven minutes past, Europe/London. Every run takes a
snapshot. Two runs per deadline send an email - **T-24h** and **T-3h** - so roughly
two a week in season.

The T-3h run is the one to act on, and this is not a detail. On the live injury
table, **23 of 44 listed players are "Currently Being Assessed"** - over half.
That status is precisely what a manager's press conference resolves, and those
pressers land in the day or two before a fixture. T-24h can fall ahead of some of
them - early enough to plan a transfer and watch a price change, but labelled
provisional for that reason. T-3h sits after them.

For a weekend round that means T-24h on Friday morning and T-3h on Saturday
morning. **That is an example, not the schedule.** Deadlines are not always Friday
or Saturday - midweek rounds put them on a Tuesday or Wednesday and the festive
period scatters them further. Both tiers are pure offsets from the deadline epoch,
so no case is special; where the pressers have already happened by T-24h, the
provisional label simply errs on the cautious side.

The T-24h email is labelled `PROVISIONAL` in an amber banner, the T-3h one
`CONFIRMED`. There was once a T-48h tier as well; it was dropped because it fired
before *any* press conference and was superseded by both of the others.

### The objective is rank, not points

This is the single most consequential modelling decision, and it is worth being
clear about because it makes the output look "wrong" if you expect a points
ranking.

Consider a 60%-owned midfielder with 6.5 expected points and a 4%-owned one with
6.0. Ranking on expected points puts the template player first. But against the
field, owning him gains you almost nothing - 60% of your rivals own him too, so
his points wash out of the comparison. His value is largely *defensive*: not
owning him is what costs you. The differential's points accrue to you and to
almost nobody else.

So the board ranks on:

```
score = xP - lambda * (ownership x xP) + mu * ceiling
```

The middle term discounts the fraction of a player's points the field already
has. The last rewards upside, because rank is gained in the tail - a green arrow
comes from a haul nobody else owned, not from a steady six.

Every recommendation therefore reports **mean, floor (P10), ceiling (P90) and
ownership**, not just a single number.

### Captaincy uses a different objective, deliberately

Reusing the transfer objective for the armband would be wrong in a specific and
costly direction. Captaincy doubles the mean *and* the variance, so:

- the **floor matters far more** - a captain blank is a *double* zero, the worst
  outcome available in a gameweek;
- **ownership is penalised far more gently** - the template captain is usually
  the template captain because he is genuinely the best option, and captaincy
  differentials lose ground faster than they gain it.

So the captain section ranks on its own objective, with two downside terms and
roughly a third of the transfer ownership penalty. [docs/MODEL.md](docs/MODEL.md)
has the full derivation.

### The wildcard squad optimises the starting XI, not all fifteen

Only eleven players score. A squad optimised on all fifteen equally spends real
money on a fifth defender who never starts, which is why every serious wildcard
draft loads the XI and fills the bench with cheap bodies. The optimiser scores a
squad on its **best legal starting XI** plus a light weight on the bench - light
rather than zero, because at exactly zero it happily benches players who cannot
play at all.

### What it deliberately does not do

- **No FPL authentication.** None. `users.premierleague.com` no longer resolves;
  auth moved to PingOne OIDC. Only public endpoints are used.
- **No knowledge of your squad.** The bot does not know your fifteen players,
  your bank or your free transfers, so it produces *candidates*, not paired
  swaps. This is a design decision, not a gap - see
  [ADR-009](docs/DECISIONS.md#adr-009-no-squad-state-in-v1). A transfer
  optimiser is a possible v2 and there is a clean seam for it.

  Two consequences worth knowing. The captain picks are drawn from the whole
  player pool, so read them as "who is worth the armband this week" - **you can
  only captain someone you already own**. And the wildcard squad is exempt from
  this limitation entirely, because a wildcard discards your existing team and
  rebuilds from scratch against a fixed budget: there is no squad state to know.
  See [ADR-018](docs/DECISIONS.md).

---

## The email

Eight sections, in this order:

1. **Header** - gameweek, deadline, hours remaining, **phase**, and a
   data-quality line naming any degraded source.
2. **Buy board** - ranked within each position, each with expected points,
   ceiling/floor, price, ownership, availability risk, a **confidence**, a
   one-line **"why"** and a **runner-up**.
3. **Captain picks** - the top five for the armband, with *doubled* expected
   points, doubled floor and ceiling, and the probability of a 20+ haul. Ranked
   on a captaincy-specific objective, not the transfer one - see below.
4. **Sell / avoid** - with the evidence that triggered each entry.
5. **Injury-signal watchlist** - transfer-flow anomalies not yet reflected in
   FPL's own `news`, with the z-score and the cross-validation state.
6. **Returning from injury** - the buy-low window, before the price moves.
7. **Best wildcard squad** - the highest-projected legal 15 buildable for
   GBP 100.0m, optimised on points from now to the end of the season.
8. **Caveats** - unresolved assessments, stale sources, unmatched players.

The guiding principle, from the spec: **every recommendation must be legible
enough to argue with.** You are competing against this bot; a pick you cannot
reason about is a pick you cannot evaluate.

---

## Data sources

| Source | Provides | Notes |
|---|---|---|
| **FPL official API** | Everything: players, fixtures, deadlines, rules | No auth. Trailing slashes required. |
| **Fantasy Football Scout** | Predicted line-ups | Carries the **exact integer join key** - see below |
| **ClubElo** | Scoreline probabilities, clean sheets | HTTP only. Best value-per-effort in the stack. |
| **PremierInjuries** | Injuries and suspensions | Permissive robots.txt. Verified current. |
| **Understat** | Expected goals | Requires `X-Requested-With`. See [docs/LEGAL.md](docs/LEGAL.md). |
| **The Odds API** | Goalscorer prices | Optional, quota-bound. Degrades to ClubElo. |
| **vaastav archive** | Historical data | **Build time only.** Never on the request path. |

Excluded, each for a verified reason: **FBref** (hard-blocked from datacentre IPs
behind a Cloudflare interactive challenge - its `robots.txt` itself returns 403),
**PhysioRoom** (stale; its live table still lists last season's clubs),
**FotMob / WhoScored / OddsPortal** (terms of service expressly prohibit
automated collection).

### The join key

The most important integration detail in the project. Fantasy Football Scout's
player photo URLs embed FPL's own `elements[].code`:

```
FFS  <img src=".../photos/players/110x140/154561.png">
FPL  elements[].code       == 154561
FPL  elements[].opta_code  == "p154561"
```

So the join is an exact integer comparison:

```python
code = int(re.search(r"/110x140/(\d+)\.png", img_src).group(1))
element_id = {e.code: e.id for e in bootstrap.elements}[code]
```

That is immune to accents, to "Son" versus "Son Heung-min", and to all fourteen
`web_name` collision groups in the live data. **Fuzzy name matching is the
fallback, not the path** - and where it is used, it requires both an absolute
score and a *margin* over the runner-up, because with three Wilsons in the pool a
95 that ties another 95 is a coin flip.

---

## Repository layout

```
deploy.sh              # the single entry point for deployment
template.yaml          # SAM: functions, table, bucket, schedules, alarms
samconfig.toml         # SAM CLI defaults
pipeline/              # CodePipeline: source -> test -> build -> dev -> approve -> prod
layer/requirements.txt # the Lambda dependency layer (the only dependency manifest)

src/fplbot/
  handlers/            # AWS entry points. Thin.
  pipeline.py          # orchestration: ingest -> score -> rank -> report -> send
  report/              # rendering and SES delivery
  domain/              # PURE LOGIC. No I/O, no AWS, no HTTP, no clock.
  sources/             # one module per provider. ALL network I/O lives here.
  models/              # pydantic schemas
  storage/             # DynamoDB and S3 adapters
  http/                # the single hardened HTTP client
  config.py            # settings and model tunables

scripts/record_fixtures.py  # record live payloads; build the awkward synthetic ones
tests/                      # 276 tests. Never touch a live API.
```

The dependency arrows only ever point downwards, and the `domain/` constraint is
the most valuable rule in the codebase: every function there can be tested with
plain Python objects and no mocking, which is where the interesting bugs live.

---

## Development

```bash
pip install -r layer/requirements.txt
pip install -e ".[dev]"

pytest                          # run the suite
pytest tests/test_scoring.py    # one module
ruff check src tests            # lint
ruff format src tests           # format
mypy src                        # types

python scripts/record_fixtures.py --synthetic-only   # rebuild test fixtures
sam local invoke PollFunction --event events/scheduled.json
```

Tests never hit live APIs. `respx` intercepts at the httpx transport layer, and
the CI test stage runs with **no AWS credentials at all** - which makes the
guarantee structural rather than a matter of discipline.

---

## Cost

About **$0.50 a month**. Roughly 750 Lambda invocations, a few thousand DynamoDB
writes, a couple of hundred megabytes in S3, and a handful of emails.

Do not spend effort optimising this. The one way to blow it up is an unbounded
`element-summary` backfill - that is one HTTP request *per player* - which is why
it lives in a separate function, on a weekly schedule, against a bounded
watchlist.

---

## Things that will surprise you

Collected here because each is counter-intuitive, each is verified, and each has
a comment at the relevant line explaining it:

- **`chance_of_playing_next_round` is `''` for a fit player, not `null`.** So
  `value or 0` marks every healthy player as 0% likely to play - a bug that
  inverts the entire recommendation set while looking like careful defensive
  coding.
- **`expected_goals` is a string. `expected_goals_per_90` is a float.** Same
  payload, same concept, different types.
- **`teams[].strength_attack_*` is zero for all twenty teams.** A model built on
  them rates every fixture identically while appearing to work perfectly.
- **All five DefCon fields are zero for every player**, including players whose
  other stats carried over. Prior-season defensive priors have to come from the
  vaastav archive.
- **In pre-season, `bootstrap-static` mixes two seasons.** `minutes`,
  `total_points`, `bps` and `starts` hold *last* season's values while `form`,
  `event_points` and all four `transfers_*` fields are zero.
- **FPL's `/updating/` maintenance page returns HTTP 200 with an HTML body.**
  Status-code checking alone does not catch it.
- **Understat 404s on every endpoint** without `X-Requested-With: XMLHttpRequest`.
  That 404 means "missing header", not "missing resource", and must never be
  retried.
- **PremierInjuries prefixes every table cell** with a hidden mobile label, so a
  naive `.text()` gives you `"StatusRuled Out"` - a plausible string that is
  quietly wrong.
- **`Potential Return` is day-first `DD/MM/YYYY`.** Parsing it month-first
  corrupts roughly 40% of dates into other perfectly valid dates. Nothing throws.

---

## Open questions

These are unresolved and are instrumented rather than guessed. See
[SPEC.md §8](SPEC.md) and the code comments at each site.

1. **`price_change_percent` semantics** - the highest-value unknown. Currently
   `'0'` for everyone. If it encodes progress towards the next price change it
   replaces most price modelling. Logged hourly from GW1 and correlated against
   `cost_change_event` transitions.
2. **Understat post-match update latency** - the two-phase schedule depends on
   it. Measure in GW1.
3. **`element-summary` `history[].transfers_in`** - per-gameweek or cumulative?
   The backfill function diffs consecutive rows and logs the verdict.
4. **DefCon thresholds** - DEF 10, MID/FWD 12 is community-sourced only. Not in
   the API.
5. **Whether `teams[].strength_attack_*` ever populate** this season.
6. **`can_transact` / `can_select` semantics** - new, and unused by FPL's own app.
7. **A JSON line-ups route at FFS `/wp-json/ffs/v1/`** - probed and logged.
8. **The Odds API authenticated response shapes** - only the 401 was confirmed.

---

## Licence and use

Personal, non-commercial. Data belongs to its respective providers; please read
[docs/LEGAL.md](docs/LEGAL.md) before pointing this at anything. Credit to
[ClubElo](http://clubelo.com), [Understat](https://understat.com),
[Fantasy Football Scout](https://www.fantasyfootballscout.co.uk),
[PremierInjuries](https://www.premierinjuries.com) and
[vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League).
