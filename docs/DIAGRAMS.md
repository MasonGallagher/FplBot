# Diagrams

Two halves, for two audiences.

**[Part 1](#part-1--the-plain-english-version)** explains what this thing is and
when it will email you, assuming no technical background and no knowledge of
Fantasy Premier League beyond "you pick footballers and get points".

**[Part 2](#part-2--the-technical-version)** is the wiring: AWS resources, the
control flow of a single run, the deployment pipeline and the module layering.

> Diagrams render automatically on GitHub. If you are reading this in a plain
> text editor you will see the Mermaid source instead, which is still readable
> top-to-bottom.

---
---

# Part 1 — the plain-English version

## What it is

Fantasy Premier League is a game where you pick a squad of real footballers and
score points based on how they actually perform each weekend. Before every round
there is a **deadline**, and before that deadline you can swap players in and
out. Choosing well is the whole game.

This is a robot that does the research for you. It watches the official Fantasy
Premier League data plus injury news and betting odds, works out which players
look like good buys, and emails you a ranked shortlist before each deadline.

**It does not play the game for you.** It does not know your team, it cannot make
transfers, and it never touches your account. It reads public information and
sends you an opinion. You decide.

## What it does, start to finish

```mermaid
flowchart LR
    A["Football data<br/><small>official FPL site, injury<br/>news, betting odds</small>"]
    B["The robot<br/><small>wakes up every hour,<br/>reads the latest data</small>"]
    C{"Is a deadline<br/>coming up soon?"}
    D["Write today's data<br/>into its diary<br/><small>and go back to sleep</small>"]
    E["Work out who to buy,<br/>sell and captain"]
    F["Email you a<br/>ranked shortlist"]

    A --> B --> C
    C -->|"No — most of the time"| D
    C -->|"Yes"| E --> F

    classDef emphasis stroke-width:3px
    class F emphasis
```

The diary matters more than it looks. Every hour it records what every player
costs and how many people are buying or selling them. That history is how it
later spots a player being quietly dumped by thousands of managers — which is
often the first sign of an injury, hours before any of it is announced.

## When the emails arrive

You get **two emails per deadline**.

```mermaid
flowchart LR
    A["Deadline is<br/>24 hours away"] --> B["📧 Email 1<br/><b>PROVISIONAL</b>"]
    B --> C["Managers hold<br/>press conferences<br/><small>injury news breaks</small>"]
    C --> D["Deadline is<br/>3 hours away"]
    D --> E["📧 Email 2<br/><b>CONFIRMED</b>"]
    E --> F["⏰ Deadline<br/><small>transfers lock</small>"]

    classDef emphasis stroke-width:3px
    class E emphasis
```

They are deliberately different, and the difference is the point:

| | When | What it is good for |
|---|---|---|
| **Provisional** | 24 hours before | Planning. Prices change overnight, so this is your chance to act early. But some injury news has not broken yet. |
| **Confirmed** | 3 hours before | **The one to act on.** Managers have given their press conferences, so who is actually fit is now known. |

If a player's fitness is still unknown at the time of writing, the email says so
rather than guessing.

## What is in the email

```mermaid
flowchart TD
    E["📧 The email"]
    E --> S1["<b>Buy board</b><br/><small>best players to bring in,<br/>ranked by position</small>"]
    E --> S2["<b>Outcome spread</b><br/><small>a chart of how risky<br/>vs. how safe each pick is</small>"]
    E --> S3["<b>Captain picks</b><br/><small>who to give the armband to<br/>— they score double</small>"]
    E --> S4["<b>Sell / avoid</b><br/><small>who to get rid of,<br/>and why</small>"]
    E --> S5["<b>Injury watchlist</b><br/><small>players being quietly sold off<br/>before any news breaks</small>"]
    E --> S6["<b>Caveats</b><br/><small>everything it is unsure about</small>"]
```

Every recommendation comes with a plain-English reason, a confidence level and a
runner-up. That is deliberate: **the aim is advice you can argue with**, not a
number you have to take on faith.

The last section is the unusual one. Most tools hide their uncertainty; this one
prints it. If a data source was down, or a player's fitness is genuinely unknown,
it says so in the email rather than quietly guessing and sounding confident.

## One thing worth understanding

The robot is **not** trying to pick the players who will score the most points.
It is trying to help you **climb the rankings**, which is a subtly different job.

```mermaid
flowchart LR
    A["Haaland<br/><small>expected: 8 points</small><br/><b>60% of players own him</b>"]
    B["A cheaper forward<br/><small>expected: 6 points</small><br/><b>4% of players own him</b>"]

    A --> A2["Everyone else<br/>has him too.<br/><small>His points barely<br/>move you up.</small>"]
    B --> B2["Almost nobody<br/>has him.<br/><small>Every point he scores<br/>is a point on your rivals.</small>"]
```

If almost everyone owns a player, his points cancel out — you gain nothing on the
field by owning him, you only *lose* by not owning him. So the robot deliberately
favours good players that few people have picked. This is why its suggestions
sometimes look "wrong" against a plain points ranking. It is not a bug.

## What it costs and where it lives

It runs on rented computing power from Amazon, waking for a few seconds each
hour. It costs roughly **£1.50 a month** — less than a coffee — and there is no
server sitting in anyone's house. If it breaks, it emails about the breakage
rather than going quiet.

---
---

# Part 2 — the technical version

## 2.1 System context

Everything outside the dashed box is somebody else's system. All of it is read
over plain HTTPS with no authentication anywhere — there is no FPL login, no
stored credentials, and no write path to any third party.

```mermaid
flowchart TB
    subgraph ext["External data sources — read-only, unauthenticated"]
        FPL["<b>FPL API</b><br/><small>bootstrap-static, fixtures,<br/>element-summary</small>"]
        ELO["<b>ClubElo</b><br/><small>scoreline distributions,<br/>clean-sheet probabilities</small>"]
        FFS["<b>Fantasy Football Scout</b><br/><small>predicted line-ups</small>"]
        PI["<b>PremierInjuries</b><br/><small>injury table</small>"]
        UND["<b>Understat</b><br/><small>xG / xA per player</small>"]
        ODDS["<b>The Odds API</b><br/><small>goalscorer prices — optional</small>"]
    end

    subgraph aws["fplBot — AWS account, eu-west-1"]
        LAMBDA["<b>Poll function</b><br/><small>ingest, score, rank, render</small>"]
        STORE[("<b>DynamoDB</b><br/><small>snapshots, locks,<br/>alias cache</small>")]
        ARCHIVE[("<b>S3</b><br/><small>raw payloads,<br/>rendered reports</small>")]
    end

    USER(["<b>You</b><br/><small>inbox</small>"])

    FPL & ELO & FFS & PI & UND & ODDS -.->|"HTTPS GET"| LAMBDA
    LAMBDA <--> STORE
    LAMBDA --> ARCHIVE
    LAMBDA -->|"SES<br/>multipart HTML + text"| USER

    classDef emphasis stroke-width:3px
    class LAMBDA emphasis
```

`FPL` is the only **required** source. Every other one is allowed to fail: each
sits behind its own circuit breaker with a last-known-good fallback, and its
absence becomes a named caveat in the email instead of an exception.

## 2.2 AWS resources

```mermaid
flowchart TB
    subgraph sched["EventBridge Scheduler — Europe/London, not UTC"]
        S1["<b>PollSchedule</b><br/><code>cron&#40;7 * * * ? *&#41;</code><br/><small>hourly at :07</small>"]
        S2["<b>BackfillSchedule</b><br/><code>cron&#40;17 3 ? * TUE *&#41;</code><br/><small>weekly</small>"]
    end

    subgraph compute["Lambda — arm64, Python 3.13"]
        F1["<b>PollFunction</b><br/><small>1024 MB · 120 s<br/>reserved concurrency 1</small>"]
        F2["<b>BackfillFunction</b><br/><small>1024 MB · 600 s<br/>element-summary history</small>"]
        LAYER["<b>DependencyLayer</b><br/><small>numpy, httpx, pydantic,<br/>orjson, selectolax — 126 MB</small>"]
    end

    subgraph data["Storage"]
        DDB[("<b>DynamoDB</b><br/><code>fplbot-state-{env}</code><br/><small>on-demand · TTL 400d · PITR</small>")]
        S3B[("<b>S3</b><br/><code>fplbot-raw-{env}-{acct}</code><br/><small>Glacier IR after 90d</small>")]
        SSM["<b>SSM Parameter Store</b><br/><small>Odds API key, SecureString</small>"]
    end

    subgraph obs["Observability"]
        CW["<b>CloudWatch</b><br/><small>logs · EMF metrics · dashboard</small>"]
        ALARMS["<b>6 alarms</b><br/><small>schema drift, invariants,<br/>errors, silence, quota, DLQ</small>"]
        SNS(["<b>SNS</b><br/><small>alarm topic</small>"])
    end

    DLQ["<b>SQS dead-letter queue</b><br/><small>14-day retention</small>"]
    SES["<b>SES</b><br/><small>verified identity</small>"]
    INBOX(["Your inbox"])

    S1 --> F1
    S2 --> F2
    S1 -. "invoke failed<br/>after retries" .-> DLQ
    S2 -. "invoke failed" .-> DLQ
    LAYER -.-> F1
    LAYER -.-> F2
    F1 <--> DDB
    F2 --> DDB
    F1 --> S3B
    F2 --> S3B
    SSM -.->|"decrypt on demand"| F1
    F1 --> CW
    F2 --> CW
    CW --> ALARMS --> SNS --> INBOX
    DLQ -.-> ALARMS
    F1 --> SES --> INBOX

    classDef emphasis stroke-width:3px
    class F1 emphasis
```

Four choices in there are load-bearing and easy to get wrong:

| Choice | Why |
|---|---|
| **EventBridge Scheduler**, not the legacy Rules API | Scheduler supports a real timezone. The legacy API is UTC-only, so a schedule silently shifts by an hour when the UK changes clocks — twice, mid-season. |
| **Reserved concurrency 1** | A scheduled singleton. Two overlapping runs would both snapshot the same hour and race on the notification lock. |
| **arm64** | ~20% cheaper per GB-second and faster for numpy. Every dependency ships an aarch64 wheel, so there is no compatibility cost. |
| **TTL 400 days**, not 30 | The snapshot series is the only training set for the price model and the only source of intra-gameweek transfer velocity. A short TTL destroys it silently and irreversibly. |

## 2.3 Control flow of one run

The shape that matters: **five cheap gates before any expensive work**. Roughly
730 invocations a month reach step 2 and stop there.

```mermaid
flowchart TD
    START(["EventBridge invoke"]) --> FETCH["Fetch FPL bootstrap-static"]
    FETCH --> ASSERT{"Timezone UTC?<br/>Invariants hold?"}
    ASSERT -->|"No"| ALARM["Emit metric<br/>→ alarm fires"]
    ASSERT -->|"Yes"| SNAP["<b>Snapshot to DynamoDB</b><br/><small>always, every run</small>"]
    ALARM --> SNAP

    SNAP --> D1{"Deadline exists?"}
    D1 -->|"No — off-season<br/>or season over"| X1(["exit: no_deadline"])
    D1 -->|"Yes"| D2{"Within 24h<br/>of a deadline?"}
    D2 -->|"No"| X2(["exit: snapshot_only"])
    D2 -->|"Yes"| D3{"Tier already<br/>notified?"}
    D3 -->|"Yes"| X3(["exit: suppressed"])
    D3 -->|"No — take lock<br/>conditional write"| WORK

    subgraph WORK["The expensive half — only when there is an email to send"]
        direction TB
        W1["Ingest third-party sources<br/><small>each behind a circuit breaker</small>"]
        W2{"Data older than<br/>the 24h ceiling?"}
        W3["Resolve player identities<br/><small>photo code → cache → exact → fuzzy</small>"]
        W4["Transfer-flow analysis<br/><small>→ availability signals</small>"]
        W5["Score every player<br/><small>Monte Carlo, 4000 samples</small>"]
        W6["Rank<br/><small>buy board · captains · wildcard</small>"]
        W7["Render HTML + plain text"]
        W1 --> W2
        W2 -->|"Yes"| FAIL["<b>Email about the FAILURE</b><br/><small>not about transfers</small>"]
        W2 -->|"No"| W3 --> W4 --> W5 --> W6 --> W7
    end

    W7 --> SEND["Archive to S3 → send via SES"]
    FAIL --> SEND
    SEND --> X4(["exit: sent"])

    classDef emphasis stroke-width:3px
    class SNAP,D3 emphasis
```

Two details worth pulling out:

**The snapshot happens before every gate.** An hour not snapshotted is lost
forever — unlike the backfill, whose data can be re-fetched retroactively. So it
runs even when the rest of the function is about to exit.

**The lock is a DynamoDB conditional write** keyed on `(season, gameweek, tier)`
and never on wall-clock time. That is what makes Scheduler's at-least-once
delivery safe: a retry has a different timestamp but the same key, so the second
write fails and no duplicate email goes out.

## 2.4 A notifying run, in sequence

```mermaid
sequenceDiagram
    autonumber
    participant SCH as Scheduler
    participant L as PollFunction
    participant D as DynamoDB
    participant EXT as External sources
    participant S3 as S3
    participant SES as SES

    SCH->>L: invoke {}
    L->>EXT: GET bootstrap-static
    EXT-->>L: 200 JSON
    L->>L: assert UTC · check invariants
    L->>S3: archive raw bytes
    L->>D: put snapshot (gzipped, ~60 KB)

    L->>L: next_deadline() · due_tier()
    L->>D: conditional put on the notification lock
    alt lock already held
        D-->>L: ConditionalCheckFailed
        L-->>SCH: exit suppressed
    else lock acquired
        D-->>L: ok
        loop each source, ≥1.5s apart, breaker-guarded
            L->>EXT: GET
            EXT-->>L: 200 · or failure → last-known-good
        end
        L->>D: read recent snapshots (transfer velocity)
        L->>L: resolve identities · score · rank · render
        L->>S3: archive rendered report
        L->>SES: SendEmail (multipart)
        SES-->>L: MessageId
        L-->>SCH: exit sent
    end
```

## 2.5 Module layering

Dependency arrows point one way only. Nothing lower reaches up.

```mermaid
flowchart TD
    H["<b>handlers/</b><br/><small>Lambda entry points — thin</small>"]
    P["<b>pipeline.py</b><br/><small>orchestration, the only place<br/>that knows the whole sequence</small>"]

    subgraph mid["Capability layer"]
        DOM["<b>domain/</b><br/><small>scoring, ranking, captaincy,<br/>deadline, availability, squad</small>"]
        SRC["<b>sources/</b><br/><small>all outbound I/O<br/>lives here and nowhere else</small>"]
        STO["<b>storage/</b><br/><small>DynamoDB, S3</small>"]
        REP["<b>report/</b><br/><small>render, email</small>"]
    end

    subgraph base["Foundation"]
        MOD["<b>models/</b><br/><small>pydantic + domain types</small>"]
        CFG["<b>config.py</b><br/><small>settings + model tunables</small>"]
        HTTP["<b>http/</b><br/><small>retry, spacing, breaker</small>"]
        OBS["<b>observability.py</b><br/><small>logger, metrics, tracer</small>"]
    end

    H --> P
    P --> DOM & SRC & STO & REP
    SRC --> HTTP
    DOM & SRC & STO & REP --> MOD & CFG & OBS

    classDef emphasis stroke-width:3px
    class DOM emphasis
```

**`domain/` is the boundary that matters.** It is pure: no network, no AWS, no
clock. Every function takes data and returns data, which is why the model can be
tested exhaustively without mocking anything, and why a squad optimiser could be
added later against the same `Distribution` objects without touching ingestion.

**All outbound I/O is in `sources/`.** One place enforces the User-Agent, the
1.5-second per-host spacing, the retry policy and the circuit breaker. A source
that bypassed it would be invisible to every one of those protections.

## 2.6 Deployment

Two CloudFormation stacks, and the separation is deliberate: the pipeline deploys
the application, and is not part of it. If they shared a stack, a broken
application template could put the pipeline that fixes it into `ROLLBACK_FAILED`.

```mermaid
flowchart LR
    DEV(["git push<br/>→ merge to main"]) --> SRC["<b>Source</b><br/><small>CodeStar connection</small>"]
    SRC --> TEST["<b>Test</b><br/><small>ruff · pytest · cfn-lint<br/><b>no AWS credentials</b></small>"]
    TEST --> BUILD["<b>Build</b><br/><small>sam build + package</small>"]
    BUILD --> DD["<b>Deploy dev</b><br/><small>DryRun=true<br/>schedules OFF</small>"]
    DD --> SMOKE["<b>Smoke test</b><br/><small>invoke dev, dry-run</small>"]
    SMOKE --> DP["<b>Deploy prod</b><br/><small>DryRun=false<br/>schedules ON</small>"]
    DP --> LIVE(["🚀 live"])

    classDef emphasis stroke-width:3px
    class TEST,DP emphasis
```

**The Test stage runs with no AWS credentials at all.** That is not an oversight —
it is the strongest available proof that the suite never touches live
infrastructure. A test that grew a dependency on real AWS would fail here,
loudly, rather than passing quietly and burning quota.

**There is no manual approval gate.** A merge reaches production unattended. What
stands between a bad commit and a live bot is the Test stage and the dev smoke
test, and those catch a crash, an import error or a broken template — they do
*not* catch a change that runs perfectly and produces worse advice. That tradeoff
is deliberate and is documented in `pipeline/pipeline.yaml`.

## 2.7 Failure handling

Every external source can fail without taking the run down.

```mermaid
flowchart TD
    REQ["Request a source"] --> BRK{"Circuit breaker<br/>open?"}
    BRK -->|"Yes — 3 consecutive<br/>failures this run"| LKG
    BRK -->|"No"| TRY["Fetch<br/><small>≥1.5s since last hit to this host</small>"]
    TRY --> OK{"200 and the<br/>expected content type?"}
    OK -->|"Yes"| USE["Use it<br/><small>archive raw bytes to S3</small>"]
    OK -->|"Retryable<br/>429/5xx"| RETRY["Backoff with full jitter<br/><small>3 attempts, cap 60s</small>"] --> TRY
    OK -->|"403 / 404<br/>never retried"| LKG
    LKG["<b>Last known good</b><br/><small>from DynamoDB</small>"] --> AGE{"Older than<br/>the 24h ceiling?"}
    AGE -->|"No"| DEG["Use it · mark source DEGRADED<br/><small>→ named in the email caveats</small>"]
    AGE -->|"Yes"| REFUSE["<b>Refuse to advise</b><br/><small>email about the failure instead</small>"]

    classDef emphasis stroke-width:3px
    class REFUSE emphasis
```

The last box is the important one. Above the staleness ceiling the bot does not
send a worse board — it sends a message saying it could not produce one, and why.
Silence would be worse than either: you would assume it ran and found nothing
worth saying.

Two policies in there are counter-intuitive and deliberate:

- **403 and 404 are never retried.** A 403 is a Cloudflare challenge, and
  hammering it looks like an attack. Understat's 404 means a missing header, and
  no amount of retrying adds one.
- **Redirects are disabled.** During maintenance the FPL API redirects to a page
  that returns **HTTP 200 with an HTML body**. A client that follows redirects
  and checks only the status code gets a parse error at best, and a
  well-formed-looking empty result at worst. The content-type assertion is the
  second half of the same defence.

---

## See also

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The same shape in prose, with more detail per component |
| [DECISIONS.md](DECISIONS.md) | Architecture decision records — the *why* behind each choice |
| [MODEL.md](MODEL.md) | How a player becomes a number, and why it is a distribution |
| [RUNBOOK.md](RUNBOOK.md) | Operating it: alarms, failure modes, what to do at 3am |
