"""One module per upstream data provider.

All network I/O in the project lives under this package, and every module here
goes through `fplbot.http.HttpClient`. Nothing calls httpx directly.

Sources present, and why each survived the cut (SPEC section 4):

    fpl               Official API. No auth needed for anything we use.
    ffs               Fantasy Football Scout predicted line-ups. Carries the
                      exact integer join key. Permissive robots.txt.
    premierinjuries   Injuries and suspensions. Explicitly permissive robots.txt.
    clubelo           Elo and exact scoreline probabilities. Best value-per-effort
                      source in the stack. No key, no rate limit.
    understat         xG. Real JSON API. robots.txt is Disallow: / - see the
                      module docstring and docs/LEGAL.md for the position taken.
    oddsapi           Optional, quota-bound. Degrades to ClubElo.
    vaastav           Historical archive. BUILD TIME ONLY - never on the request
                      path.

Sources deliberately absent, each for a verified reason:

    fbref             Hard-blocked from datacentre IPs behind a Cloudflare
                      interactive challenge. robots.txt itself returns 403.
    physioroom        Stale - its live table still lists last season's clubs.
    fotmob, whoscored, oddsportal
                      Terms of service expressly prohibit automated collection.
"""
