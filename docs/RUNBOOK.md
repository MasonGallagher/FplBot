# Runbook

Operating fplBot. Written for the person reading it at an inconvenient hour with
a deadline approaching.

---

## Normal behaviour

| When | What happens |
|---|---|
| Every hour, `:07` London | Poll runs, snapshots, usually exits `snapshot_only` |
| T-24h | Email, labelled `PROVISIONAL` - the planning report |
| T-3h | Email, labelled `CONFIRMED` - **the one to act on**, post team news |
| Every other run in the window | Lock already taken, exits `suppressed` |
| Tuesday `03:17` London | Backfill runs, ~120 players |
| Off-season | Snapshots daily, notifies never, exits `no_deadline` |

Return statuses, all of which are HTTP 200:

| Status | Meaning |
|---|---|
| `snapshot_only` | Normal. Outside a notification window. |
| `sent` | An email went out. |
| `suppressed` | Idempotency lock held - this tier was already sent. Normal. |
| `no_deadline` | Off-season or season complete. Normal. |
| `failed` | Handled failure. Check the logs; a failure email may have been sent. |

**`snapshot_only` and `no_deadline` are successes.** Doing nothing outside a
notification window is the correct behaviour.

---

## First-response commands

```bash
STACK=fplbot-prod
REGION=eu-west-1

# What happened on the last run?
sam logs --stack-name $STACK --region $REGION --tail

# Run it now
aws lambda invoke --function-name fplbot-poll-prod --region $REGION /dev/stdout

# Force a specific tier (still respects the idempotency lock)
aws lambda invoke --function-name fplbot-poll-prod --region $REGION \
  --cli-binary-format raw-in-base64-out \
  --payload '{"force_tier":"3h"}' /dev/stdout

# Recent errors only
aws logs filter-log-events --region $REGION \
  --log-group-name /aws/lambda/fplbot-poll-prod \
  --filter-pattern '{ $.level = "ERROR" }' \
  --start-time $(( ($(date +%s) - 86400) * 1000 ))

# What did the bot actually say last Thursday?
aws s3 ls s3://fplbot-raw-prod-<account>/reports/2026-27/gw12/
```

---

## Alarms

### `schema-drift`

**Means:** an upstream payload contained fields we do not know about.

**Severity:** informational, but do not ignore it. Models use `extra="allow"`, so
this cannot break a run. It matters because a new field is very often the visible
edge of a *semantic* change to an existing one.

**Do:**

```bash
aws logs filter-log-events --region $REGION \
  --log-group-name /aws/lambda/fplbot-poll-prod \
  --filter-pattern '"Unexpected field in upstream payload"'
```

Then find the raw payload in S3 (`raw/fpl/{yyyy}/{mm}/{dd}/`) and diff it against
one from before the change. This is exactly what the verbatim-bytes archive
exists for.

### `invariant-violated`

**Means:** a business rule that types cannot express has failed.

**Severity:** high. Every ownership-weighted calculation is suspect until you
understand it.

The likely culprits:

| Invariant | If it fires |
|---|---|
| `ownership_sums_to_1500` | Units changed, or the payload is partial. **Every rank-attacking calculation this run is wrong.** |
| `twenty_teams` | Partial payload, or something structural at FPL. |
| `thirty_eight_events` | Normal in July before the new season is fully published. |
| `no_gameweek_rule_overrides` | FPL changed the rules for a gameweek. Read the override; the model does not handle per-gameweek variation. |
| `unique_photo_codes` | The Fantasy Football Scout join is compromised for the affected players. |

The failure detail is also in the email's caveats section, so the reader already
knows.

### `poll-errors`

**Means:** the function raised on two consecutive hourly runs.

**Do:** read the stack trace. Genuine infrastructure failures raise; *handled*
failures return 200 with `status: failed`, so a raise here means something
unexpected.

### `poll-silent`

**Means:** no invocation for three hours.

**Severity:** high, and the most likely failure to go unnoticed - a silent bot
looks exactly like a working one.

**Do:**

```bash
aws scheduler get-schedule --name fplbot-poll-prod --region $REGION
aws sqs receive-message --region $REGION \
  --queue-url $(aws sqs get-queue-url --queue-name fplbot-schedule-dlq-prod \
                 --region $REGION --query QueueUrl --output text)
```

Usual causes: the schedule was disabled, the scheduler role was changed, or the
stack was deployed with `SchedulesEnabled=false`.

### `odds-quota-low`

**Means:** under 80 Odds API credits left this month.

**Severity:** low. Player props are skipped below the reserve floor and the run
degrades to ClubElo. The email says so in its caveats.

### `schedule-dlq`

**Means:** EventBridge Scheduler failed to invoke a function and gave up.

**Do:** read the message for the failure reason. Usually an IAM change or a
function that no longer exists.

---

## Common problems

### No email arrived

Work through in order:

1. **Did the run happen?** Check `Invocations` on the dashboard.
2. **Was it suppressed?** `status: suppressed` means the lock was held - that tier
   was already sent. Check your inbox and spam.
3. **Is SES verified?**
   ```bash
   aws sesv2 get-email-identity --email-identity you@example.com --region $REGION
   ```
   `VerifiedForSendingStatus: false` means the confirmation link was never
   clicked. AWS's verification emails land in spam with some regularity.
4. **Is `DRY_RUN` set?** Check the function's environment variables. A dev deploy
   sets it to `true` deliberately.
5. **Is SES in the sandbox?** Then every recipient must be verified individually.
   ```bash
   aws sesv2 get-account --region $REGION --query ProductionAccessEnabled
   ```

### The email arrived but a section is empty

Read the **caveats** section first - it names every degraded source. Then:

| Empty section | Likely source | Consequence |
|---|---|---|
| Buy board "why" lines lack xG detail | Understat | Falls back to FPL's Opta per-90s. Minor. |
| No clean-sheet detail | ClubElo | Falls back to FDR. Defenders are less well ranked. |
| No predicted-XI mentions | Fantasy Football Scout | Minutes model loses its freshest signal. |
| Watchlist empty | Genuinely no anomalies, **or** cold start | Check the caveat about snapshot count. |

An empty watchlist in the first few gameweeks is expected and the email says so.

### "Understat is returning 404 for everything"

Check the `X-Requested-With: XMLHttpRequest` header is being sent. Without it
**every** Understat endpoint 404s. That 404 means "missing header", not "missing
resource", and the client deliberately never retries it.

If the header is present and it still 404s, Understat may have blocked the IP
range. There is precedent. The fallback is FPL's own Opta-sourced expected goals,
which is good enough that this is a degradation and not an outage.

### "Lots of unmatched players in the caveats"

Almost always a team-name problem rather than a player-name problem, and almost
always in August.

```bash
aws logs filter-log-events --region $REGION \
  --log-group-name /aws/lambda/fplbot-poll-prod \
  --filter-pattern '"Unrecognised team name"'
```

Add the promoted clubs to `TEAM_ALIASES` in `src/fplbot/domain/teams.py` and
redeploy. This is designed to fire loudly once a season.

### "A recommendation is about the wrong player"

Check how that player was resolved:

```bash
aws dynamodb query --region $REGION --table-name fplbot-state-prod \
  --key-condition-expression "pk = :pk" \
  --expression-attribute-values '{":pk":{"S":"ALIAS#premierinjuries"}}'
```

Each entry records the `method` and `score`. A `fuzzy` match at 88 is a very
different thing from a `photo_code` match at 100.

To correct one, delete the alias and let it re-resolve, or write the right
`element_id` directly:

```bash
aws dynamodb delete-item --region $REGION --table-name fplbot-state-prod \
  --key '{"pk":{"S":"ALIAS#premierinjuries"},"sk":{"S":"4471"}}'
```

### "The bot recommended someone obviously worse"

Usually the rank-attacking objective working as designed - it deliberately
discounts template players. Read the "why" line, which states the ownership
argument explicitly.

If it genuinely looks wrong, the component breakdown is logged per player, and
the tunables are all in `ModelTunables` in `config.py`.

---

## Deliberately breaking things

Sometimes you need the bot to stop.

```bash
# Silence it without tearing anything down
./deploy.sh --env prod --no-schedules

# Keep it running but stop it emailing
./deploy.sh --env prod --dry-run

# Disable one schedule by hand
aws scheduler update-schedule --name fplbot-poll-prod --region $REGION --state DISABLED
```

To re-send a tier that was already sent, delete its lock:

```bash
aws dynamodb delete-item --region $REGION --table-name fplbot-state-prod \
  --key '{"pk":{"S":"NOTIFY#2026-27#12#3h"},"sk":{"S":"LOCK"}}'
```

Then invoke with `{"force_tier":"3h"}`.

---

## Seasonal maintenance

### Every August

1. **Update `TEAM_ALIASES`** for the promoted clubs. The invariant will tell you.
2. **Bump `SEASON`** in `.env` and redeploy. This changes the DynamoDB partition
   key prefix, so it cleanly separates seasons.
3. **Verify the season gate.** After the first deadline passes,
   `season_has_started` flips and aggregate features come alive. Check that
   attacking rates stop being pure priors.
4. **Resolve the open questions.** GW1 is when `price_change_percent`,
   Understat's update latency, `element-summary` transfer semantics and the
   DefCon thresholds all become measurable. They are instrumented and logged.

### Every gameweek, if you are being diligent

Check the logged Spearman correlation against FPL's own `ep_next`:

```bash
aws logs filter-log-events --region $REGION \
  --log-group-name /aws/lambda/fplbot-poll-prod \
  --filter-pattern '"Benchmark against FPL ep_next"'
```

**A model that cannot beat `ep_next` is not worth shipping.** This is the cheapest
possible ongoing check that it still does.

---

## Cost

Expected: about **$0.50/month**.

If it is materially higher, the cause is almost certainly one of:

- an unbounded `element-summary` backfill (check the backfill's invocation count
  and the `limit` it was called with);
- CloudWatch Logs retention (log groups are declared in the template so this
  should not happen, but Lambda creates them with never-expire retention if the
  declaration is ever removed);
- S3 lifecycle rules not applying (check the `raw/` prefix is transitioning to
  Glacier Instant Retrieval after 90 days).
