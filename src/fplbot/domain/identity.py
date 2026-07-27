"""Resolving a third-party player name to an FPL element id.

READ THIS FIRST: fuzzy matching is the **fallback**, not the path.

The primary route is an exact integer join. Fantasy Football Scout's player photo
URLs embed FPL's own `elements[].code`, and the vaastav archive carries the FPL
`element` id directly. Those two cover the sources that matter most, and they are
immune to accents, to "Son" versus "Son Heung-min", and to every collision below.
Only Understat and PremierInjuries need name matching at all.

WHY NAME MATCHING IS GENUINELY HARD HERE
----------------------------------------
Measured against the live 558-element list:

* **14 `web_name` collision groups**, including `Wilson` x3 and `Phillips` x3.
  A surname can therefore never be a key on its own. A team constraint is
  mandatory, not an optimisation.
* **`second_name` is the full legal chain.** Gabriel's is "dos Santos Magalhaes".
  Naive `first + second` concatenation fuzzy-matches badly against a source that
  writes "Gabriel".
* **Initialised names have no space after the period.** FPL writes `J.Timber`,
  which tokenises to the single token `jtimber` and matches nothing. Inserting
  the space is a one-line fix and without it that player is simply unmatchable.
* **Smart quotes.** FFS emits `O’Reilly` with U+2019; FPL uses the ASCII
  apostrophe.

THE CASCADE
-----------
Stop at the first acceptance:

1. **Deterministic** - photo code, vaastav element, opta_code. Target: >90%.
2. **Cache** - keyed by the *source's own stable id*, so each player is
   fuzzy-matched once, ever. This is what keeps the fuzzy layer's blast radius
   small: a bad match is made once and can be corrected once.
3. **Normalise** - unicode, punctuation, particles.
4. **Constrained fuzzy** - candidate pool built by team first, then position,
   then `token_set_ratio`.

`token_set_ratio` is the correct rapidfuzz scorer here, and the choice is not
arbitrary. Plain `ratio` is sunk by the extra surname tokens in the legal chain -
"Gabriel" versus "Gabriel dos Santos Magalhaes" scores poorly. `token_set_ratio`
compares the *sets* of tokens and ignores the surplus, which is exactly the
distortion we face.

AND ALWAYS REQUIRE A MARGIN
---------------------------
An absolute score is not enough. With three Wilsons in the pool, a 95 that ties
another 95 is a coin flip, and a coin flip that produces a confident
recommendation is worse than no recommendation. Ties are rejected outright.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from rapidfuzz import fuzz

from fplbot.config import TUNABLES
from fplbot.models.fpl import Element
from fplbot.observability import Metric, count, logger

# Name particles. Treated as *optional* tokens rather than deleted: "de Bruyne"
# and "Bruyne" should both match, but dropping the particle entirely would make
# "de Jong" and "Jong" indistinguishable from a hypothetical "Jong" who is a
# different player. Keeping them optional preserves the information without
# making it mandatory.
PARTICLES = {
    "de",
    "da",
    "do",
    "dos",
    "das",
    "del",
    "della",
    "van",
    "von",
    "der",
    "den",
    "di",
    "le",
    "la",
    "el",
    "al",
    "bin",
    "ibn",
    "st",
}


class ResolutionMethod(StrEnum):
    PHOTO_CODE = "photo_code"  # FFS <img src> -> elements[].code
    OPTA_CODE = "opta_code"
    VAASTAV_ELEMENT = "vaastav_element"
    CACHE = "cache"
    EXACT_NAME = "exact_name"
    FUZZY = "fuzzy"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Resolution:
    element_id: int | None
    method: ResolutionMethod
    score: float = 100.0
    runner_up_score: float = 0.0
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.element_id is not None

    @property
    def is_deterministic(self) -> bool:
        return self.method in {
            ResolutionMethod.PHOTO_CODE,
            ResolutionMethod.OPTA_CODE,
            ResolutionMethod.VAASTAV_ELEMENT,
        }


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
_INITIAL_NO_SPACE = re.compile(r"\.(?=\S)")
_NON_NAME_CHARS = re.compile(r"[^a-z\s']")
_WHITESPACE = re.compile(r"\s+")


def normalise_name(name: str) -> str:
    """Fold a name to a comparable form.

    Steps, each earning its place:

    1. **NFKD decompose and strip combining marks.** Turns "Magalhães" into
       "Magalhaes". Sources are wildly inconsistent about accents and this makes
       the question moot.
    2. **U+2019 -> ASCII apostrophe.** FFS uses the typographic one.
    3. **Insert a space after an initial's period.** `J.Timber` becomes
       `j timber` - two tokens. Without the inserted space the whole name is the
       single token `jtimber`, which matches nothing. The lookahead `(?=\\S)` is
       what stops us mangling a trailing period. (The period itself is then
       stripped by step 5; it carries no matching information once the token
       boundary exists.)
    4. **Hyphens to spaces.** "Son Heung-min" and "Son Heung min" should agree.
    5. Lowercase, strip anything that is not a letter, apostrophe or space,
       collapse whitespace.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("’", "'").replace("ʼ", "'")
    text = _INITIAL_NO_SPACE.sub(". ", text)
    text = text.replace("-", " ").replace("_", " ")
    text = text.lower()
    text = _NON_NAME_CHARS.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def name_tokens(name: str, *, drop_particles: bool = False) -> list[str]:
    """Split a normalised name into tokens, optionally without particles."""
    tokens = normalise_name(name).split()
    if drop_particles:
        stripped = [t for t in tokens if t not in PARTICLES]
        # Never return nothing - "de Jong" written as just "de" is degenerate but
        # returning an empty token list would score 100 against everything.
        return stripped or tokens
    return tokens


def candidate_strings(element: Element) -> list[str]:
    """Every name form we will score a source's name against.

    Order matters only for readability - we take the max - but `known_name` is
    first because it is FPL's own highest-precision field, populated for exactly
    the ~65 players that are hard to match. If a player has one, it is almost
    always the right thing to compare with.
    """
    forms = [
        element.known_name or "",
        element.web_name or "",
        f"{element.first_name} {element.second_name}".strip(),
        element.second_name or "",
        # Last-name-only form with particles dropped, which catches sources that
        # write "Magalhaes" for "dos Santos Magalhaes".
        " ".join(name_tokens(element.second_name or "", drop_particles=True)),
    ]
    return [f for f in forms if f]


def score_against(name: str, element: Element) -> float:
    """Best token_set_ratio of `name` against any of the element's name forms."""
    normalised = normalise_name(name)
    if not normalised:
        return 0.0
    return max(
        fuzz.token_set_ratio(normalised, normalise_name(form))
        for form in candidate_strings(element)
    )


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------
class PlayerResolver:
    """Resolves third-party player identities to FPL element ids.

    Construct once per run with the bootstrap element list and the alias cache
    already loaded, then call `resolve` per player. Lookup structures are built
    once in `__init__` rather than per call - with 558 elements and a few hundred
    resolutions, rebuilding them per call would dominate the runtime.
    """

    def __init__(
        self,
        elements: list[Element],
        *,
        element_types: dict[int, str] | None = None,
        aliases: dict[str, int] | None = None,
    ) -> None:
        self._elements = elements
        self._by_id = {e.id: e for e in elements}
        self._by_code = {e.code: e for e in elements if not e.has_temporary_code}
        self._by_opta = {
            e.opta_code: e for e in elements if e.opta_code and not e.has_temporary_code
        }
        self._aliases = dict(aliases or {})
        self._element_types = element_types or {}

        # Team-constrained pools. Building the pool by team first is what turns
        # three Wilsons into one Wilson, and it is the difference between fuzzy
        # matching being usable and being a liability.
        self._by_team: dict[int, list[Element]] = {}
        for element in elements:
            self._by_team.setdefault(element.team, []).append(element)

        # Exact normalised-name index, used before we ever reach for rapidfuzz.
        # Values are lists because collisions are the whole problem.
        self._by_exact_name: dict[str, list[Element]] = {}
        for element in elements:
            for form in candidate_strings(element):
                self._by_exact_name.setdefault(normalise_name(form), []).append(element)

        self.newly_resolved: dict[str, tuple[int, ResolutionMethod, float]] = {}
        self.unresolved: list[str] = []

    # -- deterministic paths ----------------------------------------------

    def by_photo_code(self, code: int) -> Resolution:
        """The Fantasy Football Scout join. See SPEC section 4.1.

            FFS  <img src=".../110x140/154561.png">
            FPL  elements[].code == 154561
            FPL  elements[].opta_code == "p154561"

        An exact integer join, immune to every naming problem in this module.
        This is the primary path and it should account for the overwhelming
        majority of lineup resolutions.
        """
        element = self._by_code.get(code)
        if element is None:
            return Resolution(
                None, ResolutionMethod.UNRESOLVED, 0.0, detail=f"photo code {code} not in bootstrap"
            )
        return Resolution(element.id, ResolutionMethod.PHOTO_CODE, 100.0)

    def by_opta_code(self, opta_code: str) -> Resolution:
        element = self._by_opta.get(opta_code)
        if element is None:
            return Resolution(None, ResolutionMethod.UNRESOLVED, 0.0)
        return Resolution(element.id, ResolutionMethod.OPTA_CODE, 100.0)

    # -- the full cascade --------------------------------------------------

    def resolve(
        self,
        name: str,
        *,
        source: str,
        source_id: str | None = None,
        team_id: int | None = None,
        position: str | None = None,
    ) -> Resolution:
        """Resolve one third-party player.

        Args:
            name: the source's spelling.
            source: logical source name, for the alias cache namespace.
            source_id: the source's own stable id (PremierInjuries `data-id`,
                Understat `id`). When present, the result is cached against it,
                which is what makes each player fuzzy-matched once ever.
            team_id: FPL team id. Strongly recommended - without it the candidate
                pool is all 558 players and the collision risk is much higher.
            position: GKP/DEF/MID/FWD, narrows the pool further.
        """
        # 1. Cache. Cheapest, and authoritative because a cached entry was
        #    resolved (and possibly corrected) previously.
        if source_id is not None:
            cached = self._aliases.get(str(source_id))
            if cached is not None and cached in self._by_id:
                return Resolution(
                    cached,
                    ResolutionMethod.CACHE,
                    100.0,
                    detail=f"cached against {source}:{source_id}",
                )

        # 2. Build the candidate pool, most constrained first.
        pool = self._candidate_pool(team_id, position)
        if not pool:
            self.unresolved.append(f"{name} ({source})")
            count(Metric.PLAYERS_UNRESOLVED, source=source)
            return Resolution(
                None,
                ResolutionMethod.UNRESOLVED,
                0.0,
                detail="empty candidate pool - team constraint unmatched",
            )

        # 3. Exact normalised name, within the pool.
        # Compare by element id rather than by object: pydantic's __eq__ compares
        # every field, which would turn this membership test into a deep compare
        # of 60-odd fields per candidate.
        normalised = normalise_name(name)
        pool_ids = {e.id for e in pool}
        exact = [e for e in self._by_exact_name.get(normalised, []) if e.id in pool_ids]
        if len(exact) == 1:
            element = exact[0]
            self._record(source, source_id, element.id, ResolutionMethod.EXACT_NAME, 100.0)
            return Resolution(element.id, ResolutionMethod.EXACT_NAME, 100.0)
        if len(exact) > 1:
            # A genuine collision even inside the team constraint. Do not guess.
            logger.warning(
                "Exact name collision within team constraint",
                extra={"player_name": name, "source": source, "candidates": [e.id for e in exact]},
            )

        # 4. Constrained fuzzy.
        return self._fuzzy(
            name, pool, source=source, source_id=source_id, team_constrained=team_id is not None
        )

    def _candidate_pool(self, team_id: int | None, position: str | None) -> list[Element]:
        """Team first, then position. Order matters.

        Team is the far stronger constraint: it cuts 558 players to about 28 and
        eliminates every cross-club surname collision in one step. Position then
        cuts that to a handful. Applying position first would leave ~140
        midfielders and still contain multiple Phillipses.
        """
        pool = self._by_team.get(team_id, []) if team_id is not None else list(self._elements)
        if position and self._element_types:
            narrowed = [e for e in pool if self._element_types.get(e.element_type) == position]
            # Only narrow if it leaves something. A source disagreeing with FPL
            # about whether someone is a midfielder or a forward is common and
            # should not cost us the match.
            if narrowed:
                pool = narrowed
        return pool

    def _fuzzy(
        self,
        name: str,
        pool: list[Element],
        *,
        source: str,
        source_id: str | None,
        team_constrained: bool,
    ) -> Resolution:
        scored = sorted(
            ((score_against(name, element), element) for element in pool),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best_score, best = scored[0]
        runner_up_score = scored[1][0] if len(scored) > 1 else 0.0
        margin = best_score - runner_up_score

        accept, reason = self._decide(best_score, margin, team_constrained)

        if accept:
            self._record(source, source_id, best.id, ResolutionMethod.FUZZY, best_score)
            logger.debug(
                "Fuzzy match accepted",
                extra={
                    "source": source,
                    "player_name": name,
                    "matched": best.display_name(),
                    "score": best_score,
                    "margin": margin,
                },
            )
            return Resolution(
                best.id, ResolutionMethod.FUZZY, best_score, runner_up_score, detail=reason
            )

        self.unresolved.append(f"{name} ({source}) - {reason}")
        count(Metric.PLAYERS_UNRESOLVED, source=source)
        logger.info(
            "Fuzzy match rejected",
            extra={
                "source": source,
                "player_name": name,
                "best": best.display_name(),
                "score": best_score,
                "margin": margin,
                "reason": reason,
            },
        )
        return Resolution(
            None, ResolutionMethod.UNRESOLVED, best_score, runner_up_score, detail=reason
        )

    @staticmethod
    def _decide(score: float, margin: float, team_constrained: bool) -> tuple[bool, str]:
        """The acceptance table from SPEC section 4.7.

            score  team constrained  action
            >=92   yes               auto-accept
            87-91  yes               accept iff unique best AND margin >= 6
            87-91  no                reject -> review queue
            <87    -                 skip, log

        The margin requirement applies at every level, including above 92. The
        spec's table is written as an absolute-score ladder but its own commentary
        is explicit that a 95 tying another 95 is a coin flip, and a coin flip is
        exactly what we must not turn into a confident recommendation.

        These thresholds are informed starting points and should be calibrated
        against roughly fifty hand-labelled hard cases. SPEC section 8 item 10.
        """
        if score < TUNABLES.fuzzy_review_floor:
            return False, f"score {score:.0f} below floor {TUNABLES.fuzzy_review_floor}"

        if margin < TUNABLES.fuzzy_required_margin:
            return False, (
                f"margin {margin:.0f} below required {TUNABLES.fuzzy_required_margin} "
                f"(best {score:.0f}) - ambiguous, refusing to guess"
            )

        if score >= TUNABLES.fuzzy_auto_accept:
            return True, f"score {score:.0f} with margin {margin:.0f}"

        if team_constrained:
            return True, f"score {score:.0f} accepted under team constraint, margin {margin:.0f}"

        return False, (
            f"score {score:.0f} in the review band and no team constraint - too risky without one"
        )

    def _record(
        self,
        source: str,
        source_id: str | None,
        element_id: int,
        method: ResolutionMethod,
        score: float,
    ) -> None:
        """Queue a resolution for persistence and count it."""
        if method is ResolutionMethod.FUZZY:
            count(Metric.PLAYERS_RESOLVED_FUZZY, source=source)
        else:
            count(Metric.PLAYERS_RESOLVED_EXACT, source=source)
        if source_id is not None:
            self._aliases[str(source_id)] = element_id
            self.newly_resolved[str(source_id)] = (element_id, method, score)

    # -- reporting ---------------------------------------------------------

    def resolution_rate(self, attempted: int) -> float:
        if not attempted:
            return 1.0
        return 1.0 - (len(self.unresolved) / attempted)


def find_web_name_collisions(elements: list[Element]) -> dict[str, list[int]]:
    """Which `web_name` values are shared by more than one player.

    Not used in the hot path - it exists so the diagnostics run can report the
    collision count, and so a test can assert the phenomenon still exists rather
    than us maintaining a stale claim about it in a comment.
    """
    by_name: dict[str, list[int]] = {}
    for element in elements:
        by_name.setdefault(element.web_name, []).append(element.id)
    return {name: ids for name, ids in by_name.items() if len(ids) > 1}
