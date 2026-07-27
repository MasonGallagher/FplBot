"""Model parsing - specifically the FPL type traps.

Each test here corresponds to a documented, live inconsistency in the FPL API.
They exist because every one of them fails *quietly* if handled naively: you get
a plausible number rather than an exception.
"""

from __future__ import annotations

import pytest

from fplbot.models.base import parse_chance_of_playing, parse_optional_float, parse_timestamp
from fplbot.models.fpl import Bootstrap, Element


class TestChanceOfPlaying:
    def test_empty_string_means_fully_fit(self) -> None:
        """THE trap.

        FPL uses `''` for a fit player, not null. `value or 0` therefore marks
        every healthy player as 0% likely to play - inverting the entire
        recommendation set while looking like careful defensive coding.
        """
        assert parse_chance_of_playing("") == 100

    def test_none_also_means_fit(self) -> None:
        """`chance_of_playing_this_round` is null outside a live gameweek."""
        assert parse_chance_of_playing(None) == 100

    def test_a_real_percentage_is_preserved(self) -> None:
        assert parse_chance_of_playing(25) == 25
        assert parse_chance_of_playing("50") == 50

    def test_element_exposes_it_as_a_percentage(self, bootstrap: Bootstrap) -> None:
        fit = next(e for e in bootstrap.elements if e.id == 1)
        doubtful = next(e for e in bootstrap.elements if e.id == 6)

        assert fit.availability_pct == 100
        assert doubtful.availability_pct == 25


class TestStringTypedNumbers:
    def test_ownership_is_a_string_upstream(self, bootstrap: Bootstrap) -> None:
        element = next(e for e in bootstrap.elements if e.id == 1)

        assert isinstance(element.selected_by_percent, str)
        assert element.ownership == pytest.approx(55.4)

    def test_per_90_variants_are_genuine_floats(self, bootstrap: Bootstrap) -> None:
        """Same payload, same concept, different types."""
        element = next(e for e in bootstrap.elements if e.id == 1)

        assert isinstance(element.expected_goals_per_90, float)
        assert element.xg_per_90 == pytest.approx(0.95)

    def test_optional_float_handles_the_empty_string(self) -> None:
        assert parse_optional_float("") is None
        assert parse_optional_float(None) is None
        assert parse_optional_float("3.5") == 3.5
        assert parse_optional_float("not a number") is None

    def test_price_is_tenths_of_a_million(self, bootstrap: Bootstrap) -> None:
        element = next(e for e in bootstrap.elements if e.id == 1)

        assert element.now_cost == 150
        assert element.price == 15.0


class TestTimestamps:
    def test_accepts_both_precisions(self) -> None:
        """`news_added` has microseconds; `deadline_time` does not.

        A shared parser has to accept both, so we try each format rather than
        assuming one.
        """
        with_micros = parse_timestamp("2026-08-13T09:14:33.123456Z")
        without = parse_timestamp("2026-08-15T11:00:00Z")

        assert with_micros is not None
        assert without is not None
        assert with_micros.year == 2026

    def test_returns_none_for_junk(self) -> None:
        assert parse_timestamp("") is None
        assert parse_timestamp(None) is None
        assert parse_timestamp("not a date") is None


class TestSchemaDriftTolerance:
    def test_unknown_fields_do_not_break_parsing(self) -> None:
        """`extra="allow"` - and this is the whole reason for it.

        FPL adds fields mid-season without notice. Under `extra="forbid"` the
        first such addition would hard-fail every parse and take the bot off the
        air three hours before a deadline because someone added a harmless field.
        """
        element = Element.model_validate(
            {
                "id": 1,
                "code": 1,
                "element_type": 3,
                "team": 1,
                "web_name": "Test",
                "brand_new_field_from_fpl": "surprise",
                "another_one": {"nested": True},
            }
        )

        assert element.id == 1
        assert element.model_extra is not None
        assert "brand_new_field_from_fpl" in element.model_extra

    def test_missing_optional_fields_default_sensibly(self) -> None:
        element = Element.model_validate({"id": 1, "code": 1, "element_type": 3, "team": 1})

        assert element.status == "a"
        assert element.availability_pct == 100
        assert element.ownership == 0.0


class TestTransactability:
    def test_can_transact_is_preferred_when_present(self, bootstrap: Bootstrap) -> None:
        """FPL's own answer to 'may this be bought' beats interpreting `status`."""
        blocked = next(e for e in bootstrap.elements if e.id == 9)

        assert blocked.can_transact is False
        assert blocked.is_transactable is False

    def test_falls_back_to_status_when_absent(self) -> None:
        """Recorded fixtures predating the field must still work."""
        on_loan = Element.model_validate(
            {"id": 1, "code": 1, "element_type": 3, "team": 1, "status": "n"}
        )
        available = Element.model_validate(
            {"id": 2, "code": 2, "element_type": 3, "team": 1, "status": "a"}
        )

        assert on_loan.is_transactable is False
        assert available.is_transactable is True


class TestTeamStrengths:
    def test_zero_strengths_are_reported_as_unpopulated(self, bootstrap: Bootstrap) -> None:
        """All twenty teams currently have zeroes here.

        A model built on them would rate every fixture identically while
        appearing to work perfectly, which is why this is checked at runtime
        rather than assumed.
        """
        arsenal = next(t for t in bootstrap.teams if t.id == 1)

        assert arsenal.strengths_are_populated is False


class TestLookups:
    def test_elements_by_code(self, bootstrap: Bootstrap) -> None:
        by_code = bootstrap.elements_by_code()

        assert by_code[100001].id == 1

    def test_position_resolution_uses_element_types(self, bootstrap: Bootstrap) -> None:
        types = bootstrap.element_types_by_id()
        keeper = next(e for e in bootstrap.elements if e.id == 8)

        assert keeper.position(types) == "GKP"

    def test_display_name_prefers_known_name(self) -> None:
        element = Element.model_validate(
            {
                "id": 1,
                "code": 1,
                "element_type": 3,
                "team": 1,
                "web_name": "Silva",
                "known_name": "Bernardo Silva",
            }
        )

        assert element.display_name() == "Bernardo Silva"
