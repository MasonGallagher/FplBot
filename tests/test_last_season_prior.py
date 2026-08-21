"""Building the last-season prior from real Understat objects.

This exists because the first version of that loop read `player.xg_per_90`, an
attribute UnderstatPlayer does not have. Every test passed and the function
crashed on its first real invocation in production with an AttributeError -
because nothing constructed a genuine UnderstatPlayer and pushed it through the
prior path. A stub with the wrong attribute names would have passed too, so
these use the real dataclass.
"""

from __future__ import annotations

import numpy as np

from fplbot.models.fpl import Bootstrap
from fplbot.pipeline import _build_scoring_context
from fplbot.sources.understat import UnderstatPlayer


def a_player(**over) -> UnderstatPlayer:
    defaults = {
        "understat_id": "1",
        "name": "Test Player",
        "team_title": "Arsenal",
        "position": "F S",
        "games": 30,
        "minutes": 2700,
        "goals": 20,
        "xg": 24.0,
        "npxg": 18.0,
        "assists": 5,
        "xa": 6.0,
        "shots": 90,
        "key_passes": 40,
        "xg_chain": 30.0,
        "xg_buildup": 12.0,
    }
    defaults.update(over)
    return UnderstatPlayer(**defaults)


def a_run_context(bootstrap: Bootstrap):
    from fplbot.models.domain import DataQuality, RunContext

    return RunContext(
        now_epoch=1_786_575_600,
        season="2026-27",
        gameweek=2,
        deadline_epoch=1_786_662_000,
        seconds_to_deadline=86_400,
        tier="24h",
        is_confirmed_phase=False,
        season_has_started=True,
        data_quality=DataQuality(),
    )


class TestLastSeasonPrior:
    def test_a_real_understat_player_builds_a_prior(self, bootstrap: Bootstrap) -> None:
        """The regression. Reading a non-existent attribute crashed the whole
        run, and only in production, because the prior path had no coverage."""
        element_id = bootstrap.elements[0].id

        context = _build_scoring_context(
            bootstrap,
            a_run_context(bootstrap),
            {},
            None,
            {t.id: t.singular_name_short for t in bootstrap.element_types},
            {element_id: a_player()},
        )

        assert element_id in context.prior_xg90_by_element
        assert context.prior_xg90_by_element[element_id] > 0

    def test_the_prior_uses_non_penalty_xg(self, bootstrap: Bootstrap) -> None:
        """Penalties are modelled separately via `penalties_order`, so raw xG
        would double-count designated takers - and only in the prior, which is a
        hard asymmetry to notice later."""
        element_id = bootstrap.elements[0].id
        player = a_player(xg=24.0, npxg=18.0, minutes=2700)

        context = _build_scoring_context(
            bootstrap,
            a_run_context(bootstrap),
            {},
            None,
            {t.id: t.singular_name_short for t in bootstrap.element_types},
            {element_id: player},
        )

        assert context.prior_xg90_by_element[element_id] == player.npxg_per_90
        assert context.prior_xg90_by_element[element_id] != player.xg * 90 / player.minutes

    def test_a_player_with_no_minutes_is_skipped(self, bootstrap: Bootstrap) -> None:
        element_id = bootstrap.elements[0].id

        context = _build_scoring_context(
            bootstrap,
            a_run_context(bootstrap),
            {},
            None,
            {t.id: t.singular_name_short for t in bootstrap.element_types},
            {element_id: a_player(minutes=0)},
        )

        assert element_id not in context.prior_xg90_by_element

    def test_no_last_season_data_is_not_an_error(self, bootstrap: Bootstrap) -> None:
        """The steady state once the current season stands on its own."""
        context = _build_scoring_context(
            bootstrap,
            a_run_context(bootstrap),
            {},
            None,
            {t.id: t.singular_name_short for t in bootstrap.element_types},
            None,
        )

        assert context.prior_xg90_by_element == {}
        assert isinstance(context.rng, np.random.Generator)
