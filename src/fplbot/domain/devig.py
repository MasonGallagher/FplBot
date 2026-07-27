"""Converting bookmaker odds into probabilities - "devigging".

WHY THIS MODULE EXISTS AT ALL
-----------------------------
SPEC section 4.6 says to use `penaltyblog` and not to hand-roll this. It also
says, in section 2, not to add `scipy` (121 MB unzipped) or `pandas`, and notes
that Lambda's hard limit is 250 MB unzipped across function plus layers.

Those two instructions conflict: `penaltyblog` depends on both scipy and pandas
transitively. Since the 250 MB limit is a hard platform constraint and the
library choice is not, we implement the two methods we actually need - Shin and
power - directly. They are roughly eighty lines between them and they are
verified against penaltyblog's output in `tests/test_devig.py`.

If the size budget ever loosens (a container image deployment, say), swapping
back is a two-line change confined to `implied_probabilities` below. See
docs/DECISIONS.md ADR-003.

WHAT DEVIGGING IS
-----------------
A bookmaker's decimal odds imply probabilities that sum to more than 1. The
excess is the overround, or "vig" - the margin. To recover honest probabilities
you have to remove it, and *how* you remove it is a modelling choice, not an
arithmetic one.

The naive method is multiplicative: divide each raw implied probability by the
sum. It is what almost everyone does and it is wrong in a specific, directional
way. Bookmakers do not spread margin evenly; they load it disproportionately onto
longshots, because that is where recreational money goes. Multiplicative devigging
therefore **systematically overestimates longshots.**

For us that is not an abstract concern. Anytime-goalscorer markets are exactly the
lopsided case - Haaland at ~1.6 against a full-back at ~15.0 - and overestimating
longshot scorers means over-recommending cheap differential forwards, which under
a rank-attacking objective is precisely the mistake we are most exposed to.

So: **Shin for 1X2** (documented as unbiased specifically in the EPL) and
**power for anytime-goalscorer**.

THE PARTITION TRAP
------------------
1X2 outcomes partition the sample space: home, draw and away are mutually
exclusive and exhaustive, so true probabilities sum to 1 and normalising is
meaningful.

Anytime goalscorer **is not a partition**. Each player has an independent Yes/No
pair, several players can score in the same match, and the "probabilities" across
a squad do not sum to anything in particular. Each player's Yes/No pair must be
devigged **independently**. Normalising all scorers to sum to 1.0 is a serious
error that would compress every striker towards the mean. `devig_goalscorers`
below does it correctly; there is no code path here that does it the wrong way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class DevigMethod(StrEnum):
    MULTIPLICATIVE = "multiplicative"
    SHIN = "shin"
    POWER = "power"


@dataclass(frozen=True)
class DevigResult:
    probabilities: list[float]
    overround: float
    method: DevigMethod
    # Shin's z: the estimated proportion of insider money. Typically 0.01-0.05 in
    # a liquid market. A large value means an unusually lopsided book.
    parameter: float | None = None


def raw_implied(odds: list[float]) -> list[float]:
    """Decimal odds -> raw implied probabilities (which sum to > 1)."""
    if any(o <= 1.0 for o in odds):
        raise ValueError(f"Decimal odds must exceed 1.0; got {odds}")
    return [1.0 / o for o in odds]


def overround(odds: list[float]) -> float:
    """The bookmaker's margin. 1.05 means a 5% overround."""
    return sum(raw_implied(odds))


# ---------------------------------------------------------------------------
# Multiplicative - included for comparison and for balanced two-way markets
# ---------------------------------------------------------------------------
def devig_multiplicative(odds: list[float]) -> DevigResult:
    """Divide by the sum. Simple, and biased towards longshots.

    Perfectly adequate on a balanced two-way line where both sides sit near
    even money; genuinely misleading on a lopsided one.
    """
    raw = raw_implied(odds)
    total = sum(raw)
    return DevigResult(
        probabilities=[p / total for p in raw],
        overround=total,
        method=DevigMethod.MULTIPLICATIVE,
    )


# ---------------------------------------------------------------------------
# Shin
# ---------------------------------------------------------------------------
def devig_shin(
    odds: list[float], *, tolerance: float = 1e-9, max_iterations: int = 200
) -> DevigResult:
    """Shin's (1992, 1993) method.

    The model: a bookmaker faces a proportion `z` of insider traders who know the
    outcome. To protect itself it shades the odds, and it shades them *more* on
    outcomes an insider is more likely to back - which produces exactly the
    longshot-favourite bias observed empirically.

    Given the raw implied probabilities `pi` summing to `B`, Shin's inversion is:

        p_i = ( sqrt( z^2 + 4(1-z) * pi_i^2 / B ) - z ) / ( 2(1-z) )

    and `z` is the value that makes the `p_i` sum to exactly 1. That is a
    monotonic scalar equation in `z`, so bisection solves it in a handful of
    iterations - no scipy required, and bisection cannot diverge, which matters
    for something running unattended.

    Shin is the recommended choice for 1X2 in the EPL, where it is documented as
    approximately unbiased.
    """
    raw = raw_implied(odds)
    booksum = sum(raw)

    if booksum <= 1.0:
        # No overround (or an arbitrage). Nothing to remove.
        return DevigResult(
            probabilities=[p / booksum for p in raw],
            overround=booksum,
            method=DevigMethod.SHIN,
            parameter=0.0,
        )

    def probabilities_for(z: float) -> list[float]:
        if z <= 0:
            return [p / booksum for p in raw]
        return [
            (math.sqrt(z * z + 4 * (1 - z) * (p * p) / booksum) - z) / (2 * (1 - z)) for p in raw
        ]

    # Bisect on z in [0, 1). The sum of probabilities is monotonically decreasing
    # in z, so a simple bisection is both correct and unconditionally stable.
    low, high = 0.0, 0.9999
    z = 0.0
    for _ in range(max_iterations):
        z = (low + high) / 2
        total = sum(probabilities_for(z))
        if abs(total - 1.0) < tolerance:
            break
        if total > 1.0:
            low = z
        else:
            high = z

    probabilities = probabilities_for(z)
    # Guard against tiny floating-point drift so downstream sampling never sees
    # a set that sums to 0.9999999.
    total = sum(probabilities)
    probabilities = [p / total for p in probabilities]

    return DevigResult(
        probabilities=probabilities,
        overround=booksum,
        method=DevigMethod.SHIN,
        parameter=z,
    )


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------
def devig_power(
    odds: list[float], *, tolerance: float = 1e-9, max_iterations: int = 200
) -> DevigResult:
    """The power (or "odds ratio by exponent") method.

    Find the exponent `k` such that `sum(pi_i ** k) == 1`. Because raising a
    number below 1 to a power above 1 shrinks small numbers proportionally more
    than large ones, this removes margin more aggressively from longshots than
    from favourites - the correct direction for a market where the margin is
    loaded that way.

    This is the recommended choice for anytime-goalscorer, where the price range
    within a single match spans an order of magnitude.

    `sum(pi^k)` is monotonically decreasing in `k` for `pi < 1`, so again
    bisection is safe.
    """
    raw = raw_implied(odds)
    booksum = sum(raw)

    if booksum <= 1.0:
        return DevigResult(
            probabilities=[p / booksum for p in raw],
            overround=booksum,
            method=DevigMethod.POWER,
            parameter=1.0,
        )

    low, high = 1.0, 100.0
    k = 1.0
    for _ in range(max_iterations):
        k = (low + high) / 2
        total = sum(p**k for p in raw)
        if abs(total - 1.0) < tolerance:
            break
        if total > 1.0:
            low = k
        else:
            high = k

    probabilities = [p**k for p in raw]
    total = sum(probabilities)
    probabilities = [p / total for p in probabilities]

    return DevigResult(
        probabilities=probabilities,
        overround=booksum,
        method=DevigMethod.POWER,
        parameter=k,
    )


_METHODS = {
    DevigMethod.MULTIPLICATIVE: devig_multiplicative,
    DevigMethod.SHIN: devig_shin,
    DevigMethod.POWER: devig_power,
}


def implied_probabilities(odds: list[float], method: DevigMethod = DevigMethod.SHIN) -> DevigResult:
    """Public entry point.

    If the dependency budget ever allows `penaltyblog`, this is the single
    function to swap:

        import penaltyblog as pb
        r = pb.implied.calculate_implied(odds, method=pb.implied.ImpliedMethod.SHIN)
    """
    return _METHODS[method](odds)


# ---------------------------------------------------------------------------
# Market-specific helpers
# ---------------------------------------------------------------------------
def devig_1x2(home: float, draw: float, away: float) -> tuple[float, float, float]:
    """Devig a three-way match-result market with Shin.

    These three outcomes genuinely partition the sample space, so normalising to
    1 is meaningful here - unlike goalscorer markets.
    """
    result = implied_probabilities([home, draw, away], DevigMethod.SHIN)
    p_home, p_draw, p_away = result.probabilities
    return p_home, p_draw, p_away


def devig_goalscorers(prices: dict[int, tuple[float, float]]) -> dict[int, float]:
    """Devig anytime-goalscorer prices, **one player at a time**.

    Args:
        prices: element id -> (yes_decimal_odds, no_decimal_odds).

    Returns:
        element id -> P(scores at least once).

    Each player's Yes/No pair is its own two-outcome market and is devigged
    independently with the power method. There is deliberately no code path here
    that normalises across players: anytime-goalscorer is not a partition, and
    forcing the squad to sum to 1.0 would compress every striker towards the mean
    and destroy exactly the signal we came for.

    Many books publish only the Yes price. Where `no` is absent, pass it as 0 and
    we fall back to a flat margin assumption, with the caveat that this is
    materially less accurate and should be reflected in the pick's confidence.
    """
    out: dict[int, float] = {}
    for element_id, (yes_odds, no_odds) in prices.items():
        if no_odds and no_odds > 1.0:
            result = implied_probabilities([yes_odds, no_odds], DevigMethod.POWER)
            out[element_id] = result.probabilities[0]
        else:
            # Single-sided price. Assume a typical 6% two-way margin split
            # proportionally. Crude, and flagged as such by the caller.
            out[element_id] = min(0.99, (1.0 / yes_odds) / 1.06)
    return out


def poisson_pmf(k: int, lam: float) -> float:
    """P(X = k) for a Poisson with mean lam.

    Hand-rolled because scipy is out of the dependency budget, and because for
    the goal counts we care about (0-8) the factorial is trivial. `math.lgamma`
    keeps it stable for larger k without overflowing.
    """
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(k * math.log(lam) - lam - math.lgamma(k + 1))


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k)."""
    return sum(poisson_pmf(i, lam) for i in range(k + 1))


def clean_sheet_probability_from_goals(expected_conceded: float) -> float:
    """P(concede zero) under a Poisson.

    Used only as a fallback. Where ClubElo gives us exact scoreline probabilities
    we sum those instead - a real model beats an assumed distribution, and the
    Poisson assumption is known to understate 0-0 draws slightly because goals
    within a match are not quite independent.
    """
    return poisson_pmf(0, expected_conceded)
