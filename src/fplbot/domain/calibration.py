"""Scoring our own predictions against what actually happened.

WHY THIS EXISTS
---------------
`pipeline._log_benchmark` compares our xP against FPL's `ep_next`. That is a
useful smoke test - SPEC 5.5 says a model that cannot beat `ep_next` is not worth
shipping - but it is a comparison against *another estimate*, not against truth.
Agreeing with FPL tells us we agree with FPL. It cannot tell us either of us is
right, and it cannot tell us whether the distribution is honest.

That distinction matters more here than it would in a pure expected-points model.
The ranking objective is

    score = xP - lambda * (ownership * xP) + mu * ceiling

so **the ceiling is a ranking term**. If P90 is systematically too high, the
board is ordered by a number that does not mean what it claims, and nothing in
the run would say so. The mean could be perfectly calibrated while the ordering
is driven by a miscalibrated tail.

WHAT WE MEASURE, AND WHY EACH ONE
---------------------------------
* **RMSE / MAE** - is the central estimate any good at all.
* **Spearman** - does the *ordering* work. This is the one that matters most for
  a board, because a board is a ranking; a model can be badly biased in level and
  still order players correctly, and that model is still useful.
* **Brier score on P(haul)** - are the tail probabilities honest. A model that
  says 20% and is right 20% of the time scores well; one that says 20% and is
  right 45% of the time does not, however good its mean is.
* **P10-P90 coverage** - the sharpest of the four. By construction roughly 80% of
  outcomes should land inside the stated interval. Materially less means the
  distribution is too narrow and the ceiling is overstated; materially more means
  it is too wide and the ceiling is doing nothing. Either way the `mu * ceiling`
  term is not measuring what it is supposed to.

NO SCIPY
--------
SPEC section 2 forbids it - 121 MB unzipped into a 250 MB budget. Spearman is a
Pearson correlation over ranks, and average-rank tie handling is a dozen lines of
numpy, so the dependency buys nothing here.

Everything in this module is a pure function of its arguments, like the rest of
`domain/`. No clock, no I/O, no AWS.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from fplbot.models.domain import PlayerScore

# Matches Distribution.haul_probability: P(10 or more points).
HAUL_THRESHOLD = 10.0


@dataclass(frozen=True)
class PredictionRecord:
    """What we said about one player, before the gameweek was played.

    Deliberately the distribution *summary* rather than the 4000 samples. The
    samples would be ~32 KB per player and are reproducible anyway - the RNG is
    seeded from (season, gameweek, tier) - whereas these six numbers are what
    every calibration question actually needs.
    """

    element_id: int
    mean: float
    floor: float
    ceiling: float
    haul_probability: float
    expected_minutes: float
    ownership: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PredictionRecord:
        return cls(
            element_id=int(raw["element_id"]),
            mean=float(raw["mean"]),
            floor=float(raw["floor"]),
            ceiling=float(raw["ceiling"]),
            haul_probability=float(raw["haul_probability"]),
            expected_minutes=float(raw.get("expected_minutes", 0.0)),
            ownership=float(raw.get("ownership", 0.0)),
        )


@dataclass(frozen=True)
class CalibrationReport:
    """How the predictions for one gameweek actually did."""

    gameweek: int
    tier: str
    players: int
    rmse: float
    mae: float
    spearman: float
    brier: float
    coverage: float
    mean_predicted: float
    mean_actual: float
    # Restricted to players who were predicted to start. A board is only ever
    # read for players expected to play, so including the 400 squad filler who
    # were always going to score zero flatters every metric here.
    starters_rmse: float = 0.0
    starters_spearman: float = 0.0
    starters: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_line(self) -> str:
        return (
            f"GW{self.gameweek} {self.tier}: n={self.players} "
            f"rmse={self.rmse:.2f} spearman={self.spearman:.3f} "
            f"brier={self.brier:.4f} coverage={self.coverage:.1%} "
            f"(predicted {self.mean_predicted:.2f} vs actual {self.mean_actual:.2f})"
        )


def summarise(scores: list[PlayerScore]) -> list[PredictionRecord]:
    """Reduce a scored board to the records we store for later grading."""
    return [
        PredictionRecord(
            element_id=score.element_id,
            mean=round(score.mean, 3),
            floor=round(score.floor, 3),
            ceiling=round(score.ceiling, 3),
            haul_probability=round(score.distribution.haul_probability, 4),
            expected_minutes=round(score.components.get("expected_minutes", 0.0), 2),
            ownership=round(score.ownership, 2),
        )
        for score in scores
    ]


def rank_average(values: np.ndarray) -> np.ndarray:
    """Ranks with ties averaged - the `scipy.stats.rankdata` default.

    Ties are not an edge case here. Hundreds of players share an identical
    predicted mean when they all fall back to the same positional prior, which is
    exactly the pre-season state, and ordinal ranking would impose a spurious
    ordering on them determined by nothing but array position.
    """
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=np.float64)
    ranks[order] = np.arange(1, len(values) + 1, dtype=np.float64)

    sorted_values = values[order]
    start = 0
    for index in range(1, len(sorted_values) + 1):
        if index == len(sorted_values) or sorted_values[index] != sorted_values[start]:
            if index - start > 1:
                ranks[order[start:index]] = ranks[order[start:index]].mean()
            start = index
    return ranks


def spearman(predicted: np.ndarray, actual: np.ndarray) -> float:
    """Rank correlation. Returns 0.0 when it is undefined rather than NaN.

    Undefined happens for real: if every prediction is identical - pre-season,
    everyone on the same prior - the rank vector has zero variance and the
    correlation is 0/0. A NaN would propagate into the log and the report;
    0.0 is the honest answer, meaning "these ranks carry no information".
    """
    if len(predicted) < 2:
        return 0.0
    pr = rank_average(predicted)
    ar = rank_average(actual)
    if pr.std() == 0 or ar.std() == 0:
        return 0.0
    return float(np.corrcoef(pr, ar)[0, 1])


def score_predictions(
    predictions: list[PredictionRecord],
    actuals: dict[int, int],
    *,
    gameweek: int,
    tier: str,
    starter_minutes: float = 60.0,
) -> CalibrationReport | None:
    """Grade one gameweek's predictions against the points actually scored.

    Args:
        predictions: what we said, from storage.
        actuals: element_id -> points actually scored that gameweek.
        starter_minutes: expected-minutes threshold for the "starters" cut.

    Returns None when nothing can be graded - no overlap between the two sets,
    which means either the predictions are for a different gameweek or the
    actuals have not settled. Returning None rather than a report full of zeros
    keeps a non-answer from looking like a bad score.
    """
    paired = [(p, actuals[p.element_id]) for p in predictions if p.element_id in actuals]
    if not paired:
        return None

    predicted = np.array([p.mean for p in (pair[0] for pair in paired)], dtype=np.float64)
    observed = np.array([float(pair[1]) for pair in paired], dtype=np.float64)
    floors = np.array([p.floor for p in (pair[0] for pair in paired)], dtype=np.float64)
    ceilings = np.array([p.ceiling for p in (pair[0] for pair in paired)], dtype=np.float64)
    haul_p = np.array([p.haul_probability for p in (pair[0] for pair in paired)], dtype=np.float64)
    minutes = np.array([p.expected_minutes for p in (pair[0] for pair in paired)], dtype=np.float64)

    errors = predicted - observed
    hauled = (observed >= HAUL_THRESHOLD).astype(np.float64)
    inside = (observed >= floors) & (observed <= ceilings)

    notes: list[str] = []
    coverage = float(inside.mean())
    if coverage < 0.65:
        notes.append(
            f"P10-P90 interval contained only {coverage:.0%} of outcomes, well under the ~80% "
            "it claims. The distribution is too narrow, which inflates the ceiling term the "
            "board is ranked on."
        )
    elif coverage > 0.92:
        notes.append(
            f"P10-P90 interval contained {coverage:.0%} of outcomes against a claimed ~80%. "
            "The distribution is too wide, so the ceiling is not discriminating between "
            "players the way the ranking objective assumes."
        )

    bias = float(errors.mean())
    if abs(bias) > 0.75:
        direction = "over" if bias > 0 else "under"
        notes.append(f"Mean {direction}-predicted by {abs(bias):.2f} points per player.")

    starter_mask = minutes >= starter_minutes
    starters = int(starter_mask.sum())

    return CalibrationReport(
        gameweek=gameweek,
        tier=tier,
        players=len(paired),
        rmse=float(np.sqrt(np.mean(errors**2))),
        mae=float(np.mean(np.abs(errors))),
        spearman=spearman(predicted, observed),
        brier=float(np.mean((haul_p - hauled) ** 2)),
        coverage=coverage,
        mean_predicted=float(predicted.mean()),
        mean_actual=float(observed.mean()),
        starters_rmse=(float(np.sqrt(np.mean(errors[starter_mask] ** 2))) if starters else 0.0),
        starters_spearman=(
            spearman(predicted[starter_mask], observed[starter_mask]) if starters > 1 else 0.0
        ),
        starters=starters,
        notes=notes,
    )
