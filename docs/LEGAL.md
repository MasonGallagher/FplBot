# Terms of service and data use

Personal, non-commercial use. This document states the position taken for each
source so that it is a documented judgement rather than an oversight.

---

## The general position

- **One request per source per run**, with hourly polling and 1.5-second per-host
  spacing. A full run makes perhaps fifteen requests.
- **An honest `User-Agent` with a contact URL**, so an operator who objects can
  reach us rather than silently blackholing the IP range.
- **No retry storms.** A 403 is never retried - it is a block, and retrying
  escalates it and looks like an attack.
- **No redistribution** of any third-party data.
- **Aggressive caching**, so nothing is fetched twice.

---

## Per source

### FPL official API — `fantasy.premierleague.com/api/`

Public, unauthenticated endpoints only. **No authentication of any kind**: no
login, no OIDC, no `my-team`, no stored credentials. This is out of scope by
design, and in any case `users.premierleague.com` no longer resolves.

The data is Fantasy Premier League's. We consume it for personal use and do not
republish it.

### ClubElo — `api.clubelo.com`

An explicitly public API. No key, no rate limit, and the operator publishes it
for exactly this kind of use.

**Credit ClubElo.** The email footer does.

### Fantasy Football Scout — `fantasyfootballscout.co.uk/team-news`

`robots.txt` is permissive (`Disallow:` is empty). The page is server-rendered,
has no paywall and requires no login.

One request per run. We parse it for predicted line-ups and do not republish the
page or its content.

### PremierInjuries — `premierinjuries.com/injury-table.php`

`robots.txt` is explicitly permissive (`Disallow:` empty). One request per run.

### vaastav/Fantasy-Premier-League — GitHub raw

A public, MIT-licensed archive. **Build time only** - never on the request path.
`If-None-Match` is sent so unchanged files return 304, because being a good
citizen of someone else's free CDN costs one header.

### The Odds API — `api.the-odds-api.com`

A commercial API used within its free tier under its own terms. The request plan
is fixed at 13 credits per run against a 500/month allowance, and the client
hard-stops at a reserve floor rather than running the quota to zero.

---

## Understat — a deliberate judgement call

**`understat.com/robots.txt` is `Disallow: /`.**

This is a robots prohibition, not a technical block. The repo owner has made this
call knowingly, and it is recorded here rather than buried.

**What that means.** `robots.txt` is a convention for automated crawlers, not a
contract, and it is not itself a grant or denial of legal permission. But a
blanket `Disallow: /` is a clear signal that the operator would prefer automated
clients stayed away, and it deserves to be treated as one rather than
rationalised away.

**The mitigations, all implemented:**

- **One request per gameweek per endpoint** - not per run. Roughly 38 requests a
  season.
- An honest `User-Agent` with a contact URL.
- Aggressive S3 caching, so nothing is ever fetched twice.
- Never a retry storm; a 403 is terminal.
- **Never redistributed.** The data reaches exactly one inbox.

**And critically, the fallback works.** There is precedent for Understat blocking
datacentre IPs (a `soccerdata` issue from December 2025). FPL's own
`expected_goals`, `expected_assists` and `expected_goals_conceded` are
Opta-sourced and good enough that an Understat outage degrades the model rather
than stopping it. That is verified by the fallback path in
`sources/understat.py`, not merely asserted.

**If you are forking this**, this is the one source you should make your own
decision about. Removing it is a one-line change in `pipeline.py` and the bot
continues to work.

---

## Sources deliberately excluded

Each for a specific verified reason, not from caution alone.

| Source | Reason |
|---|---|
| **FotMob** | Terms of service expressly prohibit automated collection. |
| **WhoScored** | Terms of service expressly prohibit automated collection. |
| **OddsPortal** | Terms of service expressly prohibit automated collection. |
| **FBref** | Hard-blocked from datacentre IPs behind a Cloudflare *interactive* JS challenge - `robots.txt` itself returns 403. Unusable from Lambda regardless of permission. |
| **PhysioRoom** | Not a legal matter: it is simply stale. Its live table still lists last season's clubs. |
| **X / Twitter** | API access is commercial and the terms do not suit this use. |
| **FPL Review, Fantasy Football Fix** | Commercial products. Scraping them would be taking someone's paid output. |

---

## If you operate this

- Keep the contact URL in `CONTACT_URL` pointing at something real. It is the
  mechanism by which an operator can ask you to stop.
- Do not raise the polling frequency. Above one request per five minutes to FPL
  you gain nothing - the Fastly edge TTL is 300 seconds, so you get
  byte-identical cached responses - and you cost someone else bandwidth.
- Do not remove the per-host spacing. PremierInjuries and Fantasy Football Scout
  are not Google.
- Do not republish the data.
