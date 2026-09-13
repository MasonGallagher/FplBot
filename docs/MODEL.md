# The model

How a player becomes a number, and why that number is a distribution.

> **This section is provisional, and says so.** SPEC §5 is explicit that every
> coefficient here is a starting prior awaiting a fit against historical
> gameweeks. They all live in one `ModelTunables` object in `config.py` so that
> replacing them is a single change rather than an archaeology exercise.

---

## 1. The shape

```
xP = P(appears) x [ xP_attacking + xP_defensive + xP_bonus + xP_defcon ]
     + xP_appearance
```

Rather than composing those expectations analytically, we run a **Monte Carlo**.
For each player we draw 4,000 samples, and each sample walks the whole causal
chain:

```
  minutes bucket
       |
       v
    minutes  --> appearance points
       |
       +--> goals    ~ Poisson(xG90 x strength x minutes/90)
       +--> assists  ~ Poisson(xA90 x strength x minutes/90)
       +--> conceded ~ Poisson(-ln(P(clean sheet)))
       |         |
       |         +--> clean sheet = (conceded == 0) and minutes >= 60
       |         +--> -1 per 2 conceded, for GKP and DEF
       +--> saves    ~ Poisson(...), goalkeepers only
       +--> defcon   ~ gamma-Poisson, then thresholded
       +--> bonus    conditional on returns, top ~50 players only
       +--> cards    ~ Bernoulli
```

Two reasons for sampling rather than algebra, and the first is decisive.

**The objective needs the shape, not the mean.** Under a rank-attacking objective
(§4 below) the spread *is* the signal. And the shape here is a mixture: the points
distribution conditional on "started" looks nothing like the one conditional on
"came on for the last twenty minutes". There is no tidy closed form for that
mixture, and no need for one.

**Sampling gets the correlations right for free.** Goals conceded and the clean
sheet are the same underlying event. Drawing conceded goals once and deriving the
clean sheet from it makes them consistent by construction. Compose them
analytically and you have to remember that dependency by hand, every time.

The RNG is seeded from `(season, gameweek, tier)`, so a run is reproducible: a
difference between two runs means the *data* changed, not the dice.

---

## 2. Minutes as a distribution

This is the part most FPL models get wrong, and the argument is short.

Two midfielders, both with an expected 60 minutes:

- **A** starts every week and plays 60 minutes exactly.
- **B** starts half the time (90 minutes) and is an unused substitute the rest.

Identical expected minutes. Completely different point distributions: A is a
steady 4-5, B is a bimodal mixture of 8 and 0. Under a rank-attacking objective
that difference is the entire story, because the upside tail is what gains rank
and the zero is what loses it.

So we model four buckets with probabilities, and the scorer samples a bucket
before anything else:

| Bucket | Mean minutes | Spread |
|---|---|---|
| `starter` | 84 | 9 |
| `rotation` | 55 | 20 |
| `cameo` | 18 | 11 |
| `out` | 0 | - |

The probabilities come from three sources, in increasing order of authority:

1. **Recent starts** (`starts` / games played), or - in pre-season, where those
   fields hold *last* season's values - price as a proxy for squad status. Clubs
   do not pay 12.0m for a substitute.
2. **Predicted line-ups** from Fantasy Football Scout. The freshest signal
   available, because it reflects press conferences. A named starter is floored
   at 85%.
3. **Availability**, which *caps* everything. A 25% player cannot be an 85%
   starter, whatever a line-up predicted three days ago.

Suspension short-circuits all of it: a ban is deterministic, not a fitness
question, so there is no probability to model.

---

## 3. Components

### Attacking - shrunk towards a positional prior

Small samples are the dominant early-season failure mode. **A player with 90
minutes and 1.0 xG is not a 1.0 xG/90 player** - he is a player about whom we know
almost nothing.

Empirical-Bayes shrinkage, with the prior weight expressed in "equivalent
minutes":

```
w = minutes / (minutes + 450)
estimate = w * observed + (1 - w) * prior
```

At 450 minutes (five full matches) you get a 50/50 blend. At 90 minutes you get
17% weight on the player's own record. That is intentional: it takes real
evidence to move away from the prior, and one hot afternoon is not real evidence.

Source preference: Understat's **non-penalty** xG first (penalties are modelled
separately via `penalties_order`, so counting them in the base rate would
double-count designated takers), then FPL's own Opta per-90s, then the positional
prior alone.

### Pre-season, where this matters most

`season_has_started` stays false until the **GW1 deadline passes**, so the GW1
board — the first one that counts — is built entirely from the pre-season path.

That path used to discard last season's rates outright, on the grounds that
pre-season `bootstrap-static` mixes two seasons. The caution is right about
*counters*: `minutes`, `total_points` and `bps` still hold last season's totals
while `form` and the transfer fields are zeroed. But a **per-90 rate is not
contaminated the way a total is** — and discarding it modelled Haaland at the
average forward's 0.35 xG/90 rather than his own 0.78, then let the ownership
penalty rank a 12%-owned midfielder above him for the armband.

What genuinely cannot be trusted is `minutes` as the shrinkage *weight*: a full
season of it would treat last year's form as this year's evidence. So the weight
is **capped** at `preseason_equivalent_minutes` (300 against a 450 prior, so at
most 40% on the player's own record). That does two jobs at once:

| Player | Real xG/90 | Before | After |
|---|---|---|---|
| Haaland | 0.78 | 0.35 | **0.52** |
| Gibbs-White | 0.31 | 0.17 | 0.23 |
| Isak | 0.34 | 0.35 | 0.35 |

The elite are no longer flattened, an average forward barely moves, and a small
sample is still shrunk hard — one midfielder currently shows **3.60 xG/90** off a
handful of minutes, and an uncapped rate would put him top of the board.

Deliberately conservative at 40%: quality persists across seasons, but transfers,
age and role changes make it evidence rather than fact. Like every number here it
is a prior awaiting a fit, and §8's calibration loop is what will settle it.

Where a bookmaker has priced a player's anytime-goalscorer market, that is
sharper than our xG chain - it is a liquid market's view, already devigged with
the power method. We calibrate lambda so `P(>=1 goal)` matches the market
(`lambda = -ln(1 - p)`) and blend 70/30 towards it, keeping our own minutes model
applied on top because the market prices a full 90.

### Fixture strength

Preference order, and it matters:

1. **Expected team goals** from ClubElo's scoreline distribution, or from devigged
   odds. A ratio against the league average (1.42) is directly meaningful. Bounded
   to [0.55, 1.9] - a 4.0-goal expectation should not quadruple one player's rate,
   because a rout distributes goals across a squad.
2. **FPL's `team_h_difficulty` / `team_a_difficulty`**, a populated, clean 1-5
   scale.

Conspicuously **not** in that list: `teams[].strength_attack_*`. Those are zero
for all twenty teams, so a model built on them rates every fixture identically
while appearing to work perfectly.

### Clean sheets

Straight from ClubElo: **home clean sheet = the sum of the `R:x-0` columns**. That
is exactly the FPL clean-sheet input, from a model rather than from a bookmaker's
shaded prices - and therefore already vig-free. Applying a devigging step to it
would be actively wrong.

We then calibrate the conceded-goals Poisson so that `P(0)` equals that
probability exactly:

```
lambda = -ln(P(clean sheet))
```

which preserves a real model's headline number while giving a consistent
distribution over every other scoreline.

Note that goalkeeper and defender returns within a team are **strongly
correlated**. That is a variance source, not just a shared mean, and it is why
the Monte Carlo draws the team's conceded goals once per sample.

### Defensive contribution

The insight, from SPEC §5.2: model **P(hitting the threshold)**, not the mean
rate. A player averaging 11 actions with high variance and one steady at 11 have
very different hit rates against a threshold of 10 or 12 - the volatile one clears
12 far more often, and under a threshold rule that is all that matters. Scoring on
the mean would rate them identically.

Two caveats live with the code:

- **The thresholds are UNVERIFIED.** DEF 10, MID/FWD 12 is community consensus.
  The API exposes the *points* for defensive contribution but not the thresholds.
- **Actions are over-dispersed** relative to Poisson. A side under sustained
  pressure racks up clearances in clusters, so a pure Poisson understates the tail
  and therefore the hit rate. We use a gamma-Poisson mixture (a negative binomial)
  to widen it.

All five DefCon fields are currently zero for every player, so in GW1-5 the prior
has to come from the vaastav archive.

### Bonus

Modelled only for the top ~50 players by BPS, and conditional on returns rather
than free-standing. Below that it is noise, and modelling it for everyone would
add variance without information while systematically flattering fringe players
who occasionally top a low-BPS match.

---

## 4. Availability risk

The headline feature: **transfer flow is a leading indicator of team news.** When
a player picks up a knock in training, well-connected managers transfer him out
before FPL updates `news`.

Turning that into something usable takes four pieces of care, and each is where a
naive implementation goes wrong.

### 1. Normalise by ownership. Never use absolute counts.

A 3%-owned and a 40%-owned player with the same absolute net-outflow are telling
completely different stories. We use

```
net_event / (selected_by_percent x total_players)
```

- net flow as a fraction of *current owners*. Absolute counts are dominated by
ownership and would put the same five template players at the top every week.

### 2. Z-score against the player's own baseline

Players have wildly different baseline churn. A rotation-risk midfielder is always
being shuffled; a nailed defender is not. An EWMA of the player's own recent
normalised flow is the right reference. A global distribution would flag the
volatile players every week and never flag the stable one whose sudden movement is
the actual signal.

Below 12 snapshots we return `None` rather than a number. A confident-looking
z-score derived from three observations is worse than an honest gap, because it
ends up in an email as though it meant something.

### 3. Discriminate the cause

The hard part, and the source of most false positives. A spike has at least five
plausible causes and only one is injury news:

| Cause | Signature |
|---|---|
| **Bad news** | sharp, ownership-normalised, one-directional, often out-of-hours |
| **Price bandwagon** | net **in**, correlates with `cost_change_event` momentum |
| **Fixture swing** | gradual, coincides with a fixture change, affects team-mates |
| **Post-DGW churn** | affects a whole team's players at once |
| **Chip weeks** | contaminate everything |

The benign explanations are tested **first**, so "bad news" is what remains once
everything else is ruled out, rather than the default conclusion.

The **team-mate correlation test** is the most valuable discriminator: an injury
is idiosyncratic to one player, whereas a fixture swing or post-blank churn moves
an entire club's roster together.

Out-of-hours movement strengthens the case considerably. News breaks in the
evening; routine transfer planning happens during the day.

### 4. Discount chip contamination

Wildcards inflate raw transfer counts by roughly 29% while contributing about 1.4%
of genuine transfer pressure - a wildcarding manager is rebuilding a squad, not
reacting to news about your player. `events[].chip_plays` gives us the counts to
discount by, scaled by how heavy the week actually is.

Without it, GW1-2, GW20-21 and every post-blank week read as alarming.

### And it only ever adds risk

Transfer flow can raise a player's risk but never lower it, and never to 1.0 on
its own. A managers' stampede is evidence; it is not proof, and it must not be
able to rule a fit player out.

### The cold-start caveat, stated in the email

Z-scoring needs snapshot history. Per-gameweek flows come free from
`element-summary`, but *intra-gameweek* velocity does not exist until the bot has
been running. **This feature will be weak for its first few gameweeks**, and the
email says so rather than presenting a low-confidence signal as though it were
sharp.

---

## 5. Ranking

```
score = xP - lambda * (ownership x xP) + mu * ceiling
```

with `lambda = 0.55` and `mu = 0.25` as starting priors.

A 60%-owned player with 6.5 xP loses `0.55 x 0.60 x 6.5 = 2.15` points of *rank*
value while keeping all 6.5 points of raw value. That gap is the entire point.

Ranking is done **within each position**, because squads have positional slots -
comparing a 4.0m defender with a 14.0m forward on raw xP is not a decision anyone
actually makes.

Hard filters first: `can_transact` false (FPL will not let you buy him at all),
blank gameweek (zero points, with certainty), availability risk at or above 0.95.

### Confidence is a separate axis from attractiveness

A player can be a superb pick with low confidence - a differential whose fitness
is unclear. The reader needs both, because they imply different actions: one is
"buy", the other is "wait for the T-3h report".

Confidence tracks *information quality*: sources disagreeing, an unresolved
"Currently Being Assessed", a provisional kickoff time, or a distribution too wide
relative to its mean.

---

## 6. Calibration - the part that actually matters

**Hand-picked weights are a starting prior, not the deliverable.** SPEC §5.5 is
unambiguous, and nothing above should be taken as a finding.

### Point-in-time discipline is non-negotiable

The single easiest way to build a model that looks excellent and performs
terribly is to leak post-deadline information into a pre-deadline feature: final
prices, final ownership, or *any* season-total column that includes the gameweek
being predicted. Features must be reconstructed **as they stood at the deadline**,
every time.

The vaastav archive supports this because `gws/gwN.csv` is per-gameweek and joins
on the FPL `element` id exactly.

### Evaluate on decisions, not just correlation

- Spearman correlation of predicted against actual points
- Mean absolute error
- **Decision-level metrics** - would this recommendation have gained rank?

Benchmark against FPL's own `ep_next` (free, in bootstrap), the template team,
and the overall average.

**A model that cannot beat `ep_next` is not worth shipping.** The pipeline logs
the Spearman correlation against `ep_next` on every run, so drift is visible over
a season rather than discovered in a post-mortem.

---

## 7. Captaincy

The armband is a different decision from a transfer, and using the transfer
objective for it would be wrong in a specific, costly direction.

Captaincy doubles a player's score, which amplifies the mean and the variance
together. Three consequences:

1. **The mean dominates.** Doubling makes raw expectation matter roughly twice as
   much as in any other decision.
2. **The floor matters far more.** A captain blank is a *double* zero and is the
   single most costly outcome available in a gameweek. Nothing in a transfer
   decision punishes you like it.
3. **Ownership should be penalised much more gently.** The template captain is
   usually the template captain because he is genuinely the best option. A
   captaincy differential is a high-variance rank play that loses ground faster
   than it gains it - when the template hauls and yours blanks, you drop hard,
   and that happens more often than the reverse.

So:

```
captain_score = 2*mean
              + w_ceiling * ceiling
              - w_downside * (mean - floor)
              - w_floor    * max(0, target - floor)
              - lambda_c   * (ownership x 2*mean)
```

with `lambda_c = 0.18`, about a third of the transfer value.

### Why there are two downside terms

The first implementation had only the shortfall term, `max(0, target - floor)`.
That penalty is **bounded** by `captain_floor_target` at roughly 0.9 points,
while the ceiling bonus is **unbounded**. The objective therefore could not
punish volatility at all - a 50/50 of 0 and 12 out-ranked a certain 6 at
identical mean, which is precisely backwards for the armband.

The `w_downside * (mean - floor)` term scales with the size of the downside and
is what actually does the work. The shortfall term now handles only the distinct
absolute risk of returning nothing at all.

Between them, a genuine premium - high mean, high ceiling, real variance - still
comfortably beats a safe mid-price option, because the doubled mean dominates.
What they rule out is treating a coin flip as equivalent to a certainty.

### Filters, and a caveat we state rather than hide

Hard filters are stricter than the buy board's, because the downside is doubled:
blanks excluded, and availability risk at or above **0.5** rather than 0.95.

The bot does not know your squad, so picks are drawn from the whole player pool.
**You can only captain someone you already own.** The section is best read as
"who is worth the armband this week", and the email says exactly that.

---

## 8. The wildcard squad

The best legal 15 buildable for GBP 100.0m, maximising projected points from now
to the end of the season.

### Projecting the season

The gameweek scorer answers "what will he score this Saturday". The optimiser
needs "what will he score between now and May", and running the full Monte Carlo
38 times would be both slow (91 million draws) and dishonest - it models a
specific opponent and clean-sheet probability, and none of that exists for
gameweek 31.

Instead we separate what we know from what we do not:

1. **A neutral-fixture xP per player**, from one extra Monte Carlo pass against a
   synthetic average fixture. This captures everything player-specific - minutes,
   shrunk xG, set pieces, DefCon, availability - with no fixture noise.
2. **A fixture load per gameweek**, which is genuinely knowable: how many times
   does this team play, and how hard is each one? Blanks are zero, doubles are
   two, difficulty scales each.

Season xP is the product, summed with a per-gameweek decay of 1.5%. The decay is
deliberate: undiscounted, the optimiser builds a squad around fixtures five
months away that will not survive contact with injuries, form and rescheduling.
The email states the assumption rather than burying it.

### The constraints

```
15 players: 2 GKP, 5 DEF, 5 MID, 3 FWD
total price <= 1000 (FPL's tenths of a million)
at most 3 players per club
```

### Scored on the starting XI, not all fifteen

This is what separates a useful answer from a naive one. Only eleven players
score. A squad optimised on all fifteen equally spends real money on a fifth
defender who never starts, which is why every serious wildcard draft loads the XI
and fills the bench with the cheapest legal bodies.

So a squad's value is its **best valid starting XI** plus a light weight on the
bench (0.12). Not zero: injuries, rotation and autosubs mean the bench
occasionally scores, and at exactly zero the optimiser fills it with players who
cannot play at all - which is both wrong and obviously silly when you read it.

The best XI is computed **exactly**, by enumerating every legal formation (there
are only a handful) and taking the top players per position within each.

### Solving it

A multi-dimensional knapsack, NP-hard in general, solved without a solver
dependency:

1. **Dominance pruning.** A player more expensive *and* worse than another in the
   same position can never be in an optimal squad. Provably free to discard, and
   it shrinks the space by roughly an order of magnitude. The cheap end is
   exempted, because bench fodder is chosen for price rather than points and a
   pure dominance filter would leave no affordable way to fill the bench.
2. **A cheapest-feasible seed**, so we start inside the budget and every later
   step is an improvement from a feasible point. Seeding greedily by value
   typically overspends, and repairing an infeasible squad is far fiddlier than
   improving a feasible one.
3. **Steepest-ascent local search** over single swaps.
4. **Random restarts**, keeping the best.

The email reports the spread across restarts. If they all converge within half a
point, it says "almost certainly optimal"; if they do not, it says "very good
rather than provably optimal". That is the honest version of a claim we cannot
prove.

---

## 9. Knowing whether any of this works

Every number above is a **starting prior**, not a finding. SPEC §5 says so
explicitly, and §5.5 asks for a fit against historical gameweeks. That fit is not
possible without first knowing how the current model actually performs — so this
section is about the measurement, not the model.

### The gap this closes

`pipeline._log_benchmark` compares our xP against FPL's `ep_next` on every run.
That is a useful smoke test — a model that cannot beat `ep_next` is not worth
shipping — but it compares us against **another estimate, not against truth**.
Agreeing with FPL means we agree with FPL. It cannot tell us either of us is
right, and it says nothing at all about whether the *distribution* is honest.

That last part matters more here than it would elsewhere. The ranking objective is

```
score = xP - lambda * (ownership x xP) + mu * ceiling
```

**The ceiling is a ranking term.** If P90 is systematically overstated, the board
is ordered by a number that does not mean what it claims — and the mean could be
perfectly calibrated while the ordering is driven by a miscalibrated tail.
Nothing in a run would say so.

### How it works

| Step | Where |
|---|---|
| Predictions written when a board is sent | `pipeline.py` → `store.put_predictions` |
| Actual points ride along on the hourly snapshot | `sources/fpl.py` → `event_points` |
| Graded once the gameweek settles | `handlers/backfill.py` → `_grade_predictions` |
| The metrics themselves | `domain/calibration.py` — pure functions |

One gzipped DynamoDB item per `(gameweek, tier)` rather than 600 per-player
writes, with the same 400-day TTL as the snapshot series — because the pairs of
*(what we said, what happened)* **are** the training set §5.5 needs.

Grading costs no extra HTTP. `event_points` is already on the bootstrap the poll
fetches hourly, and the poll always runs long after the last match of a gameweek
and long before the next deadline resets the field.

### The four metrics, and why each

| Metric | Answers |
|---|---|
| **RMSE / MAE** | Is the central estimate any good at all? |
| **Spearman** | Does the *ordering* work? The one that matters most — a board is a ranking, and a model can be badly biased in level while ordering players perfectly. |
| **Brier on P(haul)** | Are the tail probabilities honest? Saying 20% and being right 45% of the time is a failure however good the mean is. |
| **P10–P90 coverage** | The sharpest. ~80% of outcomes should land inside the stated interval. Materially less means the distribution is too narrow and the ceiling is overstated; materially more means the ceiling is not discriminating at all. |

Reported for all players and again for **predicted starters only**, because the
full list is dominated by squad filler who were always going to score zero, and
predicting that correctly flatters every metric.

Both tiers are graded separately. That comparison is the entire reason to store
both: if the team news available at T-3h is worth anything, it shows up as a
difference between the two scores. Collapsing them would hide the one comparison
worth making.

### Two deliberate choices

**Logged, not emitted as CloudWatch metrics.** The stack already publishes 14
custom metrics against a free tier of 10, and custom metrics are the largest line
item in a bill otherwise dominated by nothing. These fire once a week and are
read in Logs Insights when somebody is asking the question, which does not
justify a per-metric monthly charge each.

**Grading is skipped unless the current gameweek is `finished` *and*
`data_checked`.** `event_points` holds the *current* gameweek's points, so it is
only the right answer while that gameweek is still current. Grading GW7's
predictions against GW8's scoreline would look like a catastrophically bad model
rather than like a bug. `data_checked` is the stricter flag and the one that
matters: `finished` goes true at the final whistle, but bonus points land a day
or two later, so grading on `finished` alone would mark every player down by
their unawarded bonus.

### What to do with it

Wait for real data. In pre-season every attacking rate falls back to a positional
prior, so a Spearman computed now is comparing two sets of priors rather than two
models. From roughly GW5, the numbers become worth acting on — and only then does
fitting `ownership_penalty_lambda`, `ceiling_bonus_mu`, `shrinkage_prior_minutes`
and the rest against the vaastav archive become something that can be *evaluated*
rather than guessed at.
