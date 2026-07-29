"""Team name resolution.

**Never fuzzy-match team names.** There are twenty of them, they change once a
year, and the failure mode of a fuzzy match is grim: "Manchester City" and
"Manchester United" are 82% similar by token ratio, and confusing them would
corrupt every fixture, every clean-sheet probability and every candidate pool
built from a team constraint.

So: a hardcoded alias map, and an assertion at ingest that every incoming team
name is one we know. When promotion and relegation happen each August, that
assertion fires loudly on the first run and someone spends two minutes updating
this file - which is exactly the right amount of ceremony for a change that
happens three times a year.

Note the apostrophes. FPL writes `Nott'm Forest` with an ASCII apostrophe;
Fantasy Football Scout emits `O'Reilly` with U+2019, the typographic one. Both
appear below, and normalisation in `identity.py` folds them together.
"""

from __future__ import annotations

from fplbot.observability import logger

# Canonical name -> every alias we have seen in the wild, lowercased.
# Canonical names match FPL's `teams[].name`.
TEAM_ALIASES: dict[str, set[str]] = {
    "Arsenal": {"arsenal", "ars", "arsenal fc"},
    "Aston Villa": {"aston villa", "villa", "avl", "aston villa fc"},
    "Bournemouth": {"bournemouth", "afc bournemouth", "bou", "cherries"},
    "Brentford": {"brentford", "bre", "brentford fc"},
    "Brighton": {
        "brighton",
        "brighton & hove albion",
        "brighton and hove albion",
        "bha",
        "brighton hove albion",
    },
    "Burnley": {"burnley", "bur", "burnley fc"},
    "Chelsea": {"chelsea", "che", "chelsea fc"},
    "Coventry City": {"coventry city", "coventry", "cov", "coventry city fc", "ccfc"},
    "Crystal Palace": {"crystal palace", "palace", "cry", "cpfc"},
    "Everton": {"everton", "eve", "everton fc"},
    "Fulham": {"fulham", "ful", "fulham fc"},
    "Hull City": {"hull city", "hull", "hul", "hull city afc", "the tigers"},
    "Ipswich Town": {"ipswich town", "ipswich", "ips", "ipswich town fc", "itfc"},
    "Leeds": {"leeds", "leeds united", "lee", "leeds utd"},
    "Liverpool": {"liverpool", "liv", "liverpool fc"},
    "Man City": {"man city", "manchester city", "mci", "man. city", "city"},
    "Man Utd": {"man utd", "man united", "manchester united", "mun", "man u", "man. united"},
    "Newcastle": {"newcastle", "newcastle united", "new", "newcastle utd", "nufc"},
    "Nott'm Forest": {
        "nott'm forest",
        "nottingham forest",
        "forest",
        "nfo",
        "nott’m forest",
        "notts forest",
    },
    "Sunderland": {"sunderland", "sun", "sunderland afc"},
    "Spurs": {"spurs", "tottenham", "tottenham hotspur", "tot", "thfc"},
    "West Ham": {"west ham", "west ham united", "whu", "west ham utd"},
    "Wolves": {"wolves", "wolverhampton", "wolverhampton wanderers", "wol"},
}

# Flattened lookup, built once at import.
_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical for canonical, aliases in TEAM_ALIASES.items() for alias in aliases
}


def _normalise(name: str) -> str:
    """Lowercase, fold the typographic apostrophe, collapse whitespace."""
    return " ".join(name.replace("’", "'").lower().split())


def canonical_team(name: str) -> str | None:
    """Map any known alias to its canonical FPL team name, or None."""
    return _ALIAS_TO_CANONICAL.get(_normalise(name))


def resolve_team_id(name: str, teams_by_name: dict[str, int]) -> int | None:
    """Map a third-party team name to an FPL team id.

    Args:
        name: whatever the upstream source called the team.
        teams_by_name: FPL canonical name -> team id, from bootstrap.
    """
    canonical = canonical_team(name)
    if canonical and canonical in teams_by_name:
        return teams_by_name[canonical]

    # Exact match against FPL's own naming, in case the alias table is stale but
    # the source happens to use FPL's spelling.
    for fpl_name, team_id in teams_by_name.items():
        if _normalise(fpl_name) == _normalise(name):
            return team_id

    logger.warning(
        "Unrecognised team name",
        extra={
            "team_name": name,
            "hint": "Add it to TEAM_ALIASES in domain/teams.py. This fires every August "
            "when promoted clubs arrive, which is by design.",
        },
    )
    return None


def assert_known_teams(fpl_team_names: list[str]) -> list[str]:
    """Check FPL's current team list is a subset of what we know about.

    Run at ingest. Returns the unknown names so the caller can put them in the
    email's caveats section. Promotion and relegation therefore surface loudly on
    the first run of each August rather than as twenty quietly unmatched players.
    """
    unknown = [name for name in fpl_team_names if canonical_team(name) is None]
    if unknown:
        logger.error(
            "FPL team names not present in the alias map",
            extra={
                "unknown": unknown,
                "action": "Update TEAM_ALIASES in domain/teams.py - promoted clubs have arrived.",
            },
        )
    return unknown
