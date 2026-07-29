# fplBot — FPL Transfer Recommendation Lambda

## Build spec, v1

An AWS Lambda that wakes on a schedule, detects when an FPL gameweek deadline is
approaching, ingests official + third-party football data, and emails a ranked
board of players to transfer **in** and to transfer **out / avoid**.

---

## 0. READ THIS FIRST — verified facts that contradict common knowledge

This spec is the output of research that **probed the live APIs on 2026-07-25**,
rather than relying on documentation or blog posts. Several widely-repeated facts
about the FPL ecosystem are **wrong**, and the tutorials, Stack Overflow answers
and library READMEs you may have absorbed reflect the wrong version.

**Where this document contradicts your priors, this document wins.** Do not
"correct" these back toward the familiar version.

| Common belief | Reality (verified 2026-07-25) |
|---|---|
| FPL login: `POST users.premierleague.com/accounts/login/` with a `pl_profile` cookie | **`users.premierleague.com` does not resolve. NXDOMAIN.** Auth is now PingOne OIDC. Out of scope for v1 — we use only public endpoints. |
| Understat is scraped by regexing `JSON.parse('\x7B...')` out of a `<script>` tag | **Dead.** Understat has a real JSON API now. **Every endpoint returns 404 without the header `X-Requested-With: XMLHttpRequest`.** |
| FBref is a good free stats source | **Hard-blocked from datacenter IPs.** `fbref.com/robots.txt` itself returns 403 behind a Cloudflare *interactive* JS challenge. Unusable from Lambda. Excluded from v1. |
| PhysioRoom is a standard injury source | **Stale.** Its live table lists Burnley and omits the promoted clubs — it is still on 2025/26. Excluded. |
| `worldfootballR` / `understatapi` are the libraries to use | `worldfootballR` is **archived (read-only)**. `understatapi`'s last release is **2021-03-21**, predating the API migration. Both broken. |
| Player names must be fuzzy-matched to FPL ids | For our primary lineup source, **no**. There is an exact integer join — see §4.1. Fuzzy matching is the *fallback*, not the path. |
| Deadline handling needs careful timezone/DST logic | `events[].deadline_time_epoch` is a Unix int. **All deadline maths is integer comparison.** Never parse the ISO string for arithmetic. |

**A second class of trap: it is currently pre-season.** `bootstrap-static` right now
is a *mix* of 2025/26 carry-over totals and reset counters. `minutes`, `total_points`,
`bps`, `starts` hold **last season's** values; `form`, `event_points` and all four
`transfers_*` fields are **zero**. Code written and tested in July against this data
will silently mix two seasons in August. Every aggregate feature must be gated on
whether the season has actually started.

Anything below marked **[UNVERIFIED]** could not be confirmed and must be checked
against live data before being depended upon. Do not silently resolve these — surface
them.

---

## 1. Scope

**In scope (v1):**
- Detect that the next deadline is within 24h and act, once.
- Ingest FPL official API + third-party stats, odds, injury and predicted-lineup data.
- Score every player, produce a ranked **buy board** and a **sell/avoid list**.
- Email the result.

**Explicitly OUT of scope — do not build these:**
- **Any FPL authentication.** No login, no OIDC, no `my-team`, no stored credentials.
- **Squad state.** The bot does not know the user's 15 players, bank, or free
  transfers. It does **not** produce paired swap recommendations, and it does **not**
  run a squad ILP optimizer. This is deliberate. A squad-aware optimizer is a
  possible v2, gated on the user supplying their squad; leave a clean seam for it
  (§6.4) but do not implement it.
- FBref, FotMob, WhoScored, OddsPortal, X/Twitter, FPL Review, Fantasy Football Fix.
  Each is excluded for a specific verified reason (§0, §4).

**Risk stance: rank-attacking.** The objective is expected *rank* gain, not raw
expected points. Differentials and high-ceiling picks are favoured over safe
template picks. This means **ownership and outcome variance are load-bearing model
inputs, not decoration** (§5.4). The user intends to compete against these
recommendations, so every pick must carry a confidence, a runner-up, and a
human-readable "why".

---

## 2. Architecture

- **Runtime:** Python 3.13, **arm64**, zip deploy + one dependency layer.
- **IaC:** AWS SAM. Chosen for `sam local invoke` and `sam build --use-container`.
- **Dependencies:** `httpx`, `pydantic`, `orjson`, `numpy`, `selectolax` (or
  `beautifulsoup4`+`lxml`), `rapidfuzz`, `penaltyblog`, `aws-lambda-powertools`.
  - **Do NOT add `scipy`** (121 MB unzipped) — nothing in v1 needs it.
  - **Do NOT add `pandas`** or `soccerdata`. `soccerdata` assumes a persistent
    writable cache dir that Lambda does not have; read its source as a reference
    implementation for Understat, but call the endpoints directly (~60 lines).
  - Lambda's hard limit is **250 MB unzipped** across function + layers.
- **Memory** 1024 MB, **timeout** 120s (poll) / 600s (backfill, separate function).
- **Reserved concurrency: 1.** This is a scheduled singleton.
- **Region:** `eu-west-1`. Both it and `eu-west-2` sit near the Fastly LHR/LCY POPs
  fronting FPL; `eu-west-1` is the one actually deployed, and every default in the
  repo now agrees with it.

**Storage**
- **DynamoDB**, single table, on-demand, TTL enabled, PITR on.

  | Purpose | pk | sk |
  |---|---|---|
  | Bootstrap snapshot (gzipped, one item) | `SNAP#{season}` | `{iso8601}` |
  | Per-player series (watchlist only) | `PLAYER#{season}#{element_id}` | `{iso8601}` |
  | Notification idempotency lock | `NOTIFY#{season}#{gw}#{tier}` | `LOCK` |
  | Last-known-good pointer | `LKG#{season}` | `{source}` |
  | Resolved id alias cache | `ALIAS#{source}` | `{source_id}` |

  Set snapshot **TTL to 400 days, not 30**. This is the only training set for the
  price model and the only source of intra-gameweek transfer velocity. It costs cents.

- **S3:** every raw response, gzipped, **verbatim bytes — never a re-serialized parse**.
  This is what makes schema drift recoverable and backtests replayable.

**Cost:** ~$0.50/month. Do not spend effort optimizing this. The one way to blow it
up is an unbounded `element-summary` backfill — gate it to once per gameweek.

---

## 3. Scheduling — two phases, and why

**EventBridge Scheduler** (`Type: ScheduleV2` in SAM — *not* `Type: Schedule`, which
is the legacy UTC-only Rules API).

**Phase 1 — hourly poll, `cron(7 * * * ? *)`, `Europe/London`.**
Runs every hour. Always snapshots. Only *notifies* when the deadline crosses a tier.
Offset to `:07` to avoid the top-of-hour herd and let FPL's 5-minute CDN cache settle.

Polling faster than 5 minutes is **pure waste** — FPL's edge TTL is 300s, so you
will get byte-identical cached responses and a spurious velocity of zero.

**Phase 2 — a single notification at T−24h.**

This started as three tiers (48h, 24h, 3h) with T−3h as the confirmation run, which
produced three emails per gameweek and roughly 114 a season. It is now **one email
per deadline, at T−24h**, and the cost of that is worth stating rather than burying.

On the live injury table, **23 of 44 listed players are "Currently Being Assessed"** —
over half. That status is what a manager's press conference resolves, and pressers for
a Saturday fixture land Thursday–Friday afternoon. T−24h for a Saturday 11:00 deadline
is **Friday 11:00**: later than the old T−48h run, so materially better informed, but
earlier than some of those pressers. A proportion of the injury table is therefore
still unresolved when the report goes out, and **nothing follows to correct it**.

That uncertainty is carried explicitly instead of being hidden: the
`awaiting_press_conference` count is a caveat on every report — no longer suppressed
on the confirmed tier, because there is no later report to defer it to — and per-player
warnings say to confirm the presser rather than to wait for a run that will not come.

**Notification tier:** 24h. Idempotency via a DynamoDB conditional write on
`NOTIFY#{season}#{gw}#{tier}` with `ConditionExpression="attribute_not_exists(pk)"`.
Key on gameweek and tier, **never on wall-clock time**, so Scheduler retries, manual
re-invokes and at-least-once delivery are all safe.

### Deadline detection

```python
WINDOW = 48 * 3600

def next_deadline(bootstrap, now_epoch):
    events = bootstrap["events"]
    nxt = next((e for e in events if e["is_next"]), None)
    # Fallback is mandatory: is_next is None in the off-season, AND there is a
    # real window post-deadline where FPL has not yet advanced it.
    if nxt is None or nxt["deadline_time_epoch"] <= now_epoch:
        future = sorted((e for e in events if e["deadline_time_epoch"] > now_epoch),
                        key=lambda e: e["deadline_time_epoch"])
        nxt = future[0] if future else None
    if nxt is None:
        return None, None                      # season over / off-season
    return nxt, nxt["deadline_time_epoch"] - now_epoch
```

- Use `deadline_time_epoch`. Never parse `deadline_time` for arithmetic.
- `is_current` is `False` for **all** events in pre-season. Return `None`; do not
  index `[0]` into an empty filter.
- Assert `game_config.settings.timezone == "UTC"` at startup; fail loudly if it changes.
- 48h as **seconds**, not date arithmetic — sidesteps DST entirely.

**Edge cases to handle explicitly:**
- **Blank gameweeks** — teams absent from that event's fixture list. Players score 0.
  This is the highest-leverage fixture signal available and is trivial to compute.
- **Double gameweeks** — count team appearances in `fixtures/?event={gw}`; use `>= 2`,
  not `== 2` (triples exist). `len(fixtures) > 10` is neither necessary nor sufficient.
- **Postponements** — `fixtures[].event is null`, or `provisional_start_time: true`
  meaning the kickoff time is a placeholder (FPL's own UI renders "TBC").
- **Off-season** — no `is_current` and no `is_next`. Snapshot daily, notify never, exit 0.

---

## 4. Data sources

Every HTTP call goes through one client with: `Accept-Encoding: gzip`, an honest
`User-Agent` with contact URL, **`allow_redirects=False`**, a content-type assertion,
per-host serialization with ≥1.5s spacing, exponential backoff with full jitter
(base 1s, cap 60s, max 3 attempts), and a per-source circuit breaker.

**Retry only `429/500/502/503/504` and connection errors. Never retry `403` or `404`.**
A 403 is a Cloudflare challenge — retrying makes it worse and looks like an attack.
Understat's 404 means a *missing header*, not a transient fault.

### 4.0 FPL official API — `https://fantasy.premierleague.com/api/`

Trailing slashes required. No auth for anything we use.

| Endpoint | Use |
|---|---|
| `bootstrap-static/` | Everything: 558 `elements`, 38 `events`, 20 `teams`, `game_config` |
| `fixtures/` , `fixtures/?event={gw}` | Blanks, doubles, FDR, kickoff times |
| `element-summary/{id}/` | **Per-gameweek history** — see below |
| `event-status/` | Bonus/data finalization state |

**Critical field semantics (verified from FPL's own i18n strings):**
- `transfers_in_event` / `transfers_out_event` — **current gameweek only**, reset to 0
  at each deadline.
- `transfers_in` / `transfers_out` — **season cumulative**, monotonic.
- Do **not** confuse either with the price algorithm's internal counter, which resets
  on each *price change*, not each deadline, and is **not exposed by the API at all**.

**`element-summary/{id}/history[]` carries per-gameweek `transfers_in`,
`transfers_out`, `transfers_balance`, `selected` and `value`, retrievable
retroactively.** This substantially reduces the cold-start problem: gameweek-granular
transfer history does not require having been running. Only *intra-gameweek* velocity
needs our own hourly snapshots. [UNVERIFIED: whether `history[].transfers_in` is
per-gameweek or cumulative-to-that-gameweek — diff two consecutive rows to settle it.]

**Type traps:** `expected_goals`, `expected_assists`, `selected_by_percent`, `form`,
`points_per_game`, `ict_index`, `ep_this`, `ep_next` are **strings**. The `*_per_90`
variants are **floats**. Let pydantic coerce; never hand-roll `float()`.
`chance_of_playing_*` is `''` (empty string) for healthy players, **not null** —
treat `''` as 100.

**Availability fields:** `status` (`a`/`i`/`d`/`s`/`u`/`n`), `news`, `news_added`,
`chance_of_playing_next_round`. Use `chance_of_playing_next_round` as primary — FPL's
own UI reads only that one; `..._this_round` is null for all players outside a live
gameweek. Note `news_added` uses microsecond precision (`%Y-%m-%dT%H:%M:%S.%fZ`) while
`deadline_time` does not — a shared parser must accept both.

**New/undocumented fields worth using:**
- `can_transact` — hard-filter `False` out of buy candidates. Safer than `status`.
- `scout_news_link` — FPL hands you the actual club statement / reporter post behind
  each injury, keyed to element id. This largely obsoletes building club-site scrapers.
- `known_name` — populated for exactly the 65 hard-to-match players. Highest-precision
  name field; put it first in any matching candidate list.
- `price_change_percent` — currently `'0'`. **[UNVERIFIED, and the highest-value
  unknown in this spec.]** If it encodes progress toward the next price change it
  replaces most price modelling. **Log it hourly from GW1 and correlate against
  `cost_change_event` transitions. Make this a day-one instrumentation task.**

**Read constraints from `game_config.rules` at runtime, do not hardcode:**
`squad_squadsize: 15`, `squad_team_limit: 3`, `squad_total_spend: 1000`,
`transfers_sell_on_fee: 0.5`, `max_extra_free_transfers: 4` (→ 5 banked max),
`stats_form_days: 30`. This is the cheapest possible defense against a mid-season
rule change.

Note `form` is **points per game over the last 30 calendar days**, not last N matches.
It decays toward zero across international breaks for reasons unrelated to quality.

**DefCon:** `clearances_blocks_interceptions`, `recoveries`, `tackles`,
`defensive_contribution` all exist. `game_config.scoring.defensive_contribution` =
`{"DEF": 2, "FWD": 2, "GKP": 0, "MID": 2}`. **Thresholds are not in the API**
(community: DEF 10, MID/FWD 12 — **[UNVERIFIED]**, do not hardcode without a comment).

**All five DefCon fields are currently zero for every player**, including those whose
other stats carried over from 2025/26. **Prior-season DefCon priors are therefore not
obtainable from the FPL API** — source them from the vaastav archive (§4.5). This
matters for GW1–5 when in-season sample size is ~0.

**`teams[].strength_attack_*` and `strength_defence_*` are zero for all 20 teams.**
Do not build fixture difficulty on them. Use `fixtures[].team_h_difficulty` /
`team_a_difficulty` (populated, clean 1–5 scale), and ClubElo (§4.4).
[UNVERIFIED whether the attack/defence splits ever populate this season.]

### 4.1 Fantasy Football Scout predicted line-ups — THE JOIN KEY

`https://www.fantasyfootballscout.co.uk/team-news` — 200, server-rendered, no paywall,
no login, `robots.txt` permissive (`Disallow:` empty).

**The most important integration detail in this spec.** Player image URLs are
`.../photos/players/110x140/{code}.png`, and `{code}` is **exactly FPL's
`elements[].code`**:

| Source | Value |
|---|---|
| FFS `<img src>` | `.../110x140/`**`154561`**`.png` |
| FPL `elements[].code` | **`154561`** |
| FPL `elements[].opta_code` | `p`**`154561`** |

```python
code = int(re.search(r'/110x140/(\d+)\.png', img_src).group(1))
element_id = {e["code"]: e["id"] for e in bootstrap["elements"]}[code]
```

Exact integer join. Immune to accents, to `Son` vs `Son Heung-min`, to the 14
`web_name` collision groups. **Make this the primary path.** Guards: unknown code →
fall through to fuzzy matching; `has_temporary_code == true` → distrust the code.

`ul.row-1` is the GK band, `ul.row-2..N` successive outfield bands — **you get the
predicted formation for free** from row cardinalities.

**Robustness:** the CSS classes are volatile Tailwind (`class="!m-0"`). **Do not anchor
selectors on them.** Prefer regexing `110x140/(\d+)\.png` over the raw HTML and
skipping DOM parsing entirely for the primary path — that survives almost any redesign.

**Ignore the page's opening best-XI widget** — it spans multiple clubs and is not a lineup.

[UNVERIFIED: FFS is WordPress with an `ffs/v1` namespace. Probe `/wp-json/ffs/v1/` —
a JSON lineups route would be far more stable than HTML.]

### 4.2 Understat — xG

`https://understat.com`, **`X-Requested-With: XMLHttpRequest` mandatory on every call**.
League slug `EPL`; season is the starting year (`2025` = 2025/26).

- `GET /getLeagueData/{league}/{season}` — `{teams, players, dates}`
- `GET /getPlayerData/{player_id}` — per-match log, per-shot data with coordinates

`players[]` fields: `id, player_name, games, time, goals, xG, assists, xA, shots,
key_passes, position, team_title, npg, npxG, xGChain, xGBuildup`. **Every value is a
string, including floats.** But `teams[].history[]` values are real numbers — the
typing is inconsistent *within the same response*. Handle per-block.

**Parsing landmine, live right now:** for a season with no data, `teams` is an empty
**array** `[]`, not an empty object. `/getLeagueData/EPL/2026` returns
`{"teams":[],"players":[],"dates":[]}`. A `.items()` on a dict assumption throws.

`dates[].forecast` gives a free, already-vig-free `{w,d,l}` triple — a usable odds fallback.

Do a warm-up `GET https://understat.com/` first to establish cookies (this is what
`soccerdata` does, implying session gating has been seen in the wild).

**ToS:** Understat's `robots.txt` is `Disallow: /`. This is a robots prohibition, not
a technical block. Mitigate: **one request per gameweek per endpoint**, honest UA,
aggressive S3 caching, never retry-storm, never redistribute. There is precedent for
Understat blocking datacenter IPs (soccerdata issue, 2025-12), so **the fallback must
work**: FPL's own `expected_goals` / `expected_assists` / `expected_goals_conceded`
are Opta-sourced and good enough that an Understat outage is not a build-stopper.

[UNVERIFIED: post-match update latency (community estimate 2–6h). **Measure in GW1** —
the two-phase schedule depends on it.]

### 4.3 PremierInjuries — injuries and suspensions

`https://www.premierinjuries.com/injury-table.php`. `robots.txt` is explicitly
permissive (`Disallow:` empty). Verified current for 2026/27.

Structure: rows are **flat siblings**, not nested. `tr.heading[data-team-id]` opens a
team; `tr.player-row.team_{id}` belongs to it — prefer the `team_{id}` class so each
row is self-describing and you do not depend on document order.

**Every `<td>` is prefixed by `<div class="mob-title">Label</div>`.** Remove that node
before taking text or every field is polluted (`"PlayerRyan Christie"`). Do not use
naive `.text`.

Parse the repeated `tr.sub-head` row as the schema and build a label→index map;
do not hardcode column indices.

- `a.track[data-type="player"]` has `data-id` (**stable PremierInjuries player id** —
  persist it as the alias-cache anchor so each player is fuzzy-matched **once, ever**)
  and `data-name` (clean, unpolluted — prefer over `<td>` text).
- `Potential Return` is **`DD/MM/YYYY`, day-first.** Parsing as US month-first
  silently corrupts every date with day ≤ 12.

**Controlled vocabularies — assert on these and alarm on unknown values:**
- `Status` → `Ruled Out` | `25%` | `50%` | `75%`. Maps ~1:1 onto
  `chance_of_playing_next_round`.
- `Condition` → `Currently Being Assessed` | `Not Available`. **`Currently Being
  Assessed` is the "presser will resolve this" flag — it defines the Phase 2 re-check set.**
- `Reason` includes `Suspended` — bans ride in the same table as injuries. Do **not**
  model a suspension's "return probability" as if it were a fitness question.

### 4.4 ClubElo — fixture difficulty and clean sheets

`http://api.clubelo.com` — **HTTP only; HTTPS is connection-refused.** No key, no
rate limit, CSV. Best value-per-effort source in the stack.

- `GET /{YYYY-MM-DD}` → `Rank,Club,Country,Level,Elo,From,To`. Filter
  `Country == "ENG" AND Level == "1"` → exactly the 20 PL teams.
- `GET /Fixtures` → 44 columns: goal-difference distribution plus **exact scoreline
  probabilities** `R:0-0` … `R:6-0`.

Derivations, **already vig-free** (these are model probabilities, not bookmaker prices):
- **Home clean sheet** = `Σ R:x-0` = `R:0-0 + R:1-0 + ... + R:6-0`.
  **Away clean sheet** = `Σ R:0-y`. This is *exactly* the FPL clean-sheet input, free.
- 1X2 = sum GD columns by sign. Over/under = sum scorelines by total goals.

Because it is plain HTTP, a MITM could alter it: **sanity-bound Elo to ~1000–2300 and
reject out-of-range rows.** `/Fixtures` is global — filter `Country == "ENG"`.

### 4.5 vaastav/Fantasy-Premier-League — history, build-time only

`https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{season}/...`
Note the default branch is **`master`**, not `main`.

Confirmed active (pushed 2026-07-20). `gws/gwN.csv` has an `element` column — the FPL
id, so **exact join, no name matching**. Contains the DefCon columns, which is how we
get prior-season defensive priors that the FPL API no longer exposes.

**Never on the request path.** Backtest and model-training only. `fixtures.csv` embeds
the FPL `stats` blob as a **Python repr string** (single quotes) — use
`ast.literal_eval`, not `json.loads`. No `2026-27` directory yet; tolerate its absence.
GitHub raw supports `ETag` — use `If-None-Match`.

### 4.6 The Odds API — optional, quota-bound

`https://api.the-odds-api.com/v4/`, sport key `soccer_epl`. Free tier **500
credits/month**; 1 credit per region per market.

**Budget carefully — player props require one call per event:**
```
1 × /odds?regions=uk&markets=h2h_3_way,totals   =  2 credits (all fixtures)
1 × /events                                      =  1
10 × /events/{id}/odds?markets=player_goal_scorer_anytime = 10
                                            total = 13 credits/run, ~104/month
```
**Use exactly one region** — `regions=uk,eu,us` triples cost for near-identical prices.
Pull `totals`/`h2h_3_way` from the cheap featured endpoint (3 credits for all 10
matches), never per-event (30 credits).

Read `x-requests-remaining` on every call, log it, and **hard-stop at a reserve floor**.
Circuit-break independently so quota exhaustion degrades gracefully to ClubElo.

**Devigging** — use `penaltyblog`, do not hand-roll:
```python
import penaltyblog as pb
r = pb.implied.calculate_implied(odds, method=pb.implied.ImpliedMethod.SHIN)
```
Shin for 1X2 (documented as unbiased specifically in the EPL); **power for
anytime-goalscorer**. The method choice is immaterial on balanced lines but diverges
by several points on lopsided ones — and goalscorer markets are exactly that case
(Haaland ~1.6 vs a full-back ~15.0). Multiplicative there **systematically
over-estimates longshot scorers**, i.e. over-recommends cheap differential forwards.

**Anytime-goalscorer is not a partition** — outcomes are Yes/No per player and do not
sum to 1 across a squad. Devig each player's Yes/No pair **independently**. Naively
normalizing all scorers to 1.0 is a serious error.

[UNVERIFIED: all authenticated response shapes — only the 401 was confirmed. No
`clean_sheet` market key was found; use ClubElo.]

### 4.7 Name → element id (fallback path only)

Needed for Understat and PremierInjuries, which have no code join.

**Why it is hard, measured on the live 558 elements:** 14 `web_name` collision groups
including `Wilson` ×3 and `Phillips` ×3 — **surname alone can never be a key, a team
constraint is mandatory.** `second_name` is the full legal chain (`Gabriel` →
`dos Santos Magalhães`), so naive `first + second` fuzzy-matches badly. Initialised
names (`J.Timber`) have no space after the period and tokenize to `jtimber`, matching
nothing.

**Cascade, stop at first accept:**
1. **Deterministic** — FFS photo code, vaastav `element`, `opta_code`. Target >90%.
2. **Cache** every resolution in DynamoDB keyed by the *source's own stable id*
   (PremierInjuries `data-id`, Understat `id`). Each player is fuzzy-matched **once, ever.**
   This is what keeps the fuzzy layer's blast radius small.
3. **Normalize** — NFKD strip combining marks; U+2019 → `'` (FFS emits `O’Reilly` with
   a smart quote); `\.(?=\S)` → `. `; hyphens → spaces. Treat particles
   (`de`, `da`, `dos`, `van`) as optional tokens rather than deleting them.
4. **Constrained fuzzy** — build the candidate pool **by team first, then position**,
   then `rapidfuzz.fuzz.token_set_ratio` (correct choice: it tolerates the extra
   surname tokens that sink `ratio`). Score against `known_name`, `web_name`,
   `first+second`, `second_name` and take the max.

| Score | Team constrained | Action |
|---|---|---|
| ≥92 | yes | Auto-accept, cache |
| 87–91 | yes | Accept only if unique best **and** margin over runner-up ≥6 |
| 87–91 | no | Reject → review queue |
| <87 | — | Skip player, log |

**Always require a margin, not just an absolute score.** With `Wilson` ×3 in the pool,
a 95 that ties another 95 is a coin flip. Reject ties outright.
[Thresholds are informed starting points — calibrate against ~50 hand-labelled hard cases.]

**Never fuzzy-match team names.** 20 rows — hardcode the alias map (`Man Utd` /
`Man United` / `Manchester United`; `Spurs` / `Tottenham` / `Tottenham Hotspur`;
`Nott'm Forest` — ASCII apostrophe — / `Forest` / `Nottingham Forest`). Assert the
incoming set is a subset at ingest, so promotion/relegation surfaces loudly each August.

---

## 5. The model

> **STATUS: PROVISIONAL.** This section is a starting prior pending a dedicated
> research pass on published FPL analytics and optimization literature. Build §§1–4
> and §6 first; treat every coefficient here as a placeholder to be replaced by
> **fitted** values (§5.5). Structure the code so weights live in one config object,
> not scattered through the scoring functions.

### 5.1 Shape

Expected points decompose per player per fixture:

```
xP = P(appears) × [ xP_attacking + xP_defensive + xP_bonus + xP_defcon ] + xP_appearance
```

Model **minutes as a distribution, not a point estimate** — `{starter, rotation,
cameo, out}` with probabilities — because the variance matters as much as the mean
under a rank-attacking objective (§5.4). Collapsing to an expected-minutes scalar
destroys exactly the information the objective needs.

Per-fixture inputs: opponent, home/away, and for DGW players, **sum across both
fixtures**; for blanks, zero.

### 5.2 Components

- **Attacking** — per-90 xG/xA from Understat (fallback: FPL's Opta xG), **shrunk
  toward a position prior**. Small samples are the dominant early-season failure mode:
  a player with 90 minutes and 1 xG is not a 1.0 xG/90 player. Use empirical-Bayes
  shrinkage with the prior weight expressed in "equivalent minutes" — do not trust a
  raw per-90 below ~450 minutes. Scale by opponent strength and venue.
- **Team goals** — from odds (supremacy + totals → Poisson/Dixon-Coles) or ClubElo
  scorelines. Split into player shares via share-of-team-xG, with penalty and
  set-piece duty as explicit multipliers (`penalties_order == 1` is worth a large,
  separate bump — FPL's `*_order` integers are the usable signal; the `*_text` fields
  are empty strings for every player).
- **Clean sheets** — `Σ R:x-0` straight from ClubElo. Applies to GK and DEF, and note
  GK/DEF returns within a team are **strongly correlated** — this is a variance
  source, not just a mean.
- **DefCon** — model `P(hitting threshold)` from per-90 defensive-contribution rates,
  not from the mean rate. A player averaging 11 with high variance and a player
  steady at 11 have very different hit rates against a threshold of 10 or 12. This is
  a recently-introduced and still under-exploited scoring route — treat it as a real edge.
- **Bonus** — BPS is predictable enough to be worth modelling for the top ~50 players;
  ignore below that.

### 5.3 Availability risk (headline feature — the user asked for this specifically)

Produce a composite `availability_risk ∈ [0,1]` that multiplies into xP.

**Transfer-flow as a leading indicator.** `transfers_out_event` spikes *before* FPL
updates `news` or `chance_of_playing`. To make it usable:

1. **Normalize by ownership, never use absolute counts.** A 3%-owned and a 40%-owned
   player at the same absolute net-out are telling completely different stories.
   Use `net_event / (selected_by_percent × total_players)` — net flow as a fraction
   of *current owners*.
2. **Z-score against a rolling per-player baseline** (EWMA over recent snapshots), not
   against the global distribution.
3. **Discriminate the spike's cause** — this is the hard part, and where a naive
   implementation produces false positives:
   - *Bad news* → sharp, ownership-normalized, **one-directional**, often out-of-hours.
   - *Price-rise bandwagon* → net **in**, correlates with `cost_change_event` momentum.
   - *Fixture-driven* → gradual, coincides with a fixture swing, affects teammates too.
   - *Post-DGW/blank churn* → affects a whole team's players at once.
   - **Chip weeks contaminate everything.** Wildcards inflate raw transfer counts ~29%
     while contributing ~1.4% of real pressure. Use `events[].chip_plays` to build a
     per-gameweek discount. `transfers_in_event` is a **raw** count and systematically
     overstates pressure in GW1–2, GW20–21 and post-blank weeks.
4. **Cross-validate** against `chance_of_playing_next_round`, `news_added` timestamp
   recency, `status`, and PremierInjuries `Status`/`Condition`. Agreement across two
   independent signals should sharply raise confidence; disagreement should lower it
   and be surfaced in the report rather than silently resolved.
5. **The return signal** — `transfers_in` acceleration + rising minutes +
   `chance_of_playing` stepping 25→50→75→100 is the buy-low window. Worth flagging
   explicitly as its own report section.

**Cold-start caveat that must be stated in the output:** z-scoring needs snapshot
history. Per-gameweek flows come free from `element-summary` history (§4.0), but
*intra-gameweek* velocity does not exist until the bot has been running. **The
injury-inference feature will be weak for its first few gameweeks.** Say so in the
email rather than presenting a low-confidence signal as if it were sharp.

### 5.4 Ranking objective — rank-attacking

Do **not** rank on mean xP. Rank on expected *rank* gain, which requires ownership:

- A high-xP, 60%-owned player gains you almost nothing against the field — the field
  already owns him. His xP is mostly **defensive** value.
- A slightly-lower-xP, 4%-owned player with a comparable ceiling is worth more rank.

Run a **Monte Carlo** over the player-outcome distributions to get a full distribution
per candidate, not a point estimate. Score roughly:

```
score = xP − λ·(ownership × xP) + μ·ceiling
```
with `λ, μ` exposed as tunables. Report **mean, ceiling (P90), floor (P10), and
ownership** for every recommendation — under a rank-attacking stance the user needs
the spread, not just the mean.

### 5.5 Calibration — the part that actually matters

**Hand-picked weights are a starting prior, not the deliverable.** Fit them against
historical gameweeks from the vaastav archive.

**Point-in-time discipline is non-negotiable.** The single easiest way to build a
model that looks excellent and performs terribly is to leak post-deadline information
into a pre-deadline feature — final prices, final ownership, or *any* season-total
column that includes the gameweek being predicted. Reconstruct features **as they
stood at the deadline**, every time.

Evaluate on: Spearman correlation of predicted vs actual points, MAE, and — most
importantly — **decision-level metrics** (would this recommendation have gained rank?).
Benchmark against FPL's own `ep_next` (free, in bootstrap), the template team, and the
overall average. **A model that cannot beat `ep_next` is not worth shipping.**

---

## 6. Output

### 6.1 Email via SES

Sections, in order:
1. **Header** — gameweek, deadline, hours remaining, **phase (provisional at 48h vs
   the only report for this deadline)**, and a data-quality line naming any degraded source.
2. **Buy board** — ranked by position, each with: xP, ceiling/floor, price, ownership,
   availability risk, **confidence**, **a one-line "why"**, and a **runner-up**.
3. **Sell / avoid** — players with elevated availability risk, adverse fixtures, or
   declining underlying numbers. Each with the evidence that triggered it.
4. **Injury-signal watchlist** — transfer-flow anomalies not yet reflected in FPL's
   `news`, with the z-score and the cross-validation state. Include `scout_news_link`
   where present so the user can read the primary source.
5. **Returning-from-injury watchlist.**
6. **Caveats** — unresolved "Currently Being Assessed" players, stale sources,
   unmatched players, cold-start warnings.

The user is competing against this bot. **Every recommendation must be legible enough
to argue with** — a pick with no "why" is a pick they cannot reason about, which
defeats the purpose.

### 6.2 Failure policy

**Always produce output.** A recommendation from stale data, clearly flagged, beats
silence three hours before a deadline. Refuse only above a hard staleness ceiling
(~24h), and then email about the *failure*, not about transfers.

Per-source circuit breakers degrade individual features, not the whole run.

**The `/updating/` trap:** during maintenance, FPL redirects to a page that returns
**HTTP 200 with an HTML body**. Status-code checking alone does not catch this.
Mandatory: `allow_redirects=False`, treat any 3xx as failure, and assert
`content-type` is JSON before parsing.

### 6.3 Schema drift

Pydantic with **`extra="allow"`** on every model. FPL adds fields mid-season without
notice (`can_transact`, `price_change_percent`, `scout_news_link` are all recent) and
strict validation would hard-fail on harmless additions. Require only the fields whose
absence genuinely breaks scoring; everything else `| None` with a default.

Log unexpected extras once per field per deploy as a CloudWatch metric and alarm on
it — **that metric is the early-warning system that tells you FPL changed something
before it breaks you.**

**Assert business invariants that types cannot catch:**
- `sum(selected_by_percent) ≈ 1500 ± 50` (verified 1499.5 — a cheap, beautiful canary
  that ownership data is coherent)
- `len(teams) == 20`, `len(events) == 38`
- `all(e["overrides"]["rules"] == {} for e in events)` — a non-empty `overrides`
  means FPL changed the rules for that gameweek
- Per-source schema assertions: Understat `players[0]` has all 18 expected keys;
  ClubElo `/Fixtures` has all 44 columns; PremierInjuries `Status` ∈ the closed set.

**Silent schema drift is a bigger risk than outright outage**, because it yields
confident, wrong recommendations.

### 6.4 v2 seam

Structure scoring so a squad-aware optimizer can be added later without a rewrite:
keep `score_player(player, fixture, context) -> Distribution` pure and separate from
ranking. A future ILP over `{buy, sell, hold}` with budget / 3-per-club / formation /
free-transfer constraints should consume those distributions unchanged. **Do not build
it now.**

---

## 7. Build order

1. **Scaffold** — SAM template, arm64, layer, structured logging, `sam local invoke`
   working end to end against a stub.
2. **FPL client** — fetch, validate, snapshot to S3 + DynamoDB. Deadline detection
   with blanks/doubles/off-season. **This is the foundation; get it right first.**
3. **Fixture recording** — `scripts/record_fixtures.py` dumping every endpoint to a
   timestamped dir. **Do this before writing model logic.** You are building in
   pre-season against data that looks materially different in August; recorded
   fixtures are the only way to test August behaviour in July. Hand-craft synthetic
   DGW / blank / postponed / off-season / `updating.html` fixtures now — those branches
   are the most likely to be wrong and the least likely to be exercised before they matter.
4. **Third-party ingestion** — ClubElo first (easiest, highest value), then FFS
   lineups (the code join), then PremierInjuries, then Understat. Each behind a
   circuit breaker with a tested fallback.
5. **ID resolution layer** + alias cache + resolution-rate metrics.
6. **Scoring model** — §5, config-driven weights.
7. **Availability risk** — §5.3.
8. **Email rendering** + SES + idempotency locks.
9. **Backtest harness** — vaastav data, point-in-time discipline, benchmark vs `ep_next`.

**Tests must never hit live APIs.** Replay recorded fixtures with `respx`.
Snapshot-test pydantic models against every fixture.

---

## 8. Open questions — verify, do not guess

Surface these; do not silently resolve them.

1. **`price_change_percent` semantics** — highest-value unknown. Instrument from GW1.
2. **Understat post-match update latency** — measure in GW1; Phase 2 timing depends on it.
3. **`element-summary/{id}/history[].transfers_in`** — per-gameweek or cumulative?
4. **DefCon thresholds** — DEF 10 / MID+FWD 12 is community-sourced only.
5. **Whether `teams[].strength_attack_*` ever populate** this season.
6. **`can_transact` / `can_select` semantics** — new, unused by FPL's own web app.
7. **FFS `/wp-json/ffs/v1/`** — is there a JSON lineups route?
8. **The Odds API authenticated response shapes** — only the 401 was confirmed.
9. **`fixtures[].stats[]` shape** — empty pre-season; capture a real sample at GW1.
10. **rapidfuzz thresholds** — calibrate against hand-labelled hard cases.

---

## 9. Legal / ToS posture

State plainly in the README:
- **Understat `robots.txt` is `Disallow: /`.** We use it at one request per gameweek
  with an honest UA and a working fallback. This is a judgment call the repo owner
  has made knowingly, not an oversight.
- **FotMob, WhoScored, OddsPortal are excluded** — their ToS expressly prohibit
  automated collection.
- **PremierInjuries, FFS, ClubElo, GitHub raw** are all either explicitly permissive
  or public APIs.
- Do not redistribute bulk third-party data. Credit ClubElo.
- Personal, non-commercial use.
