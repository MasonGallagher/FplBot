"""Player identity resolution.

The tests fall into three groups, matching the cascade:

* **normalisation** - the specific unicode and punctuation traps;
* **deterministic joins** - the photo-code path that should carry most traffic;
* **constrained fuzzy** - and, crucially, that it *refuses* to guess when
  ambiguous. A refusal is a feature: an ambiguous match that becomes a confident
  recommendation is worse than no recommendation at all.
"""

from __future__ import annotations

from fplbot.domain.identity import (
    PlayerResolver,
    ResolutionMethod,
    find_web_name_collisions,
    name_tokens,
    normalise_name,
    score_against,
)
from fplbot.models.fpl import Bootstrap


class TestNormalisation:
    def test_strips_accents(self) -> None:
        """Sources are wildly inconsistent about accents; NFKD makes it moot."""
        assert normalise_name("Magalhães") == "magalhaes"
        assert normalise_name("Ødegaard") == "degaard"  # slash-O has no NFKD decomposition
        assert normalise_name("Doku") == "doku"

    def test_folds_the_typographic_apostrophe(self) -> None:
        """FFS emits U+2019; FPL uses ASCII. They must compare equal."""
        assert normalise_name("O’Reilly") == normalise_name("O'Reilly")

    def test_inserts_a_space_after_an_initial(self) -> None:
        """THE trap that makes initialised names unmatchable.

        `J.Timber` tokenises to the single token `jtimber` and matches nothing.
        One regex fixes it, and without it that player simply cannot be resolved.
        The period itself is then stripped - what matters is the token boundary.
        """
        assert normalise_name("J.Timber") == "j timber"
        assert name_tokens("J.Timber") == ["j", "timber"]

        # The failure mode this prevents, made explicit.
        assert name_tokens("J.Timber") != ["jtimber"]

    def test_hyphens_become_spaces(self) -> None:
        assert normalise_name("Son Heung-min") == "son heung min"

    def test_particles_can_be_dropped_optionally(self) -> None:
        """Optional, not deleted - dropping them entirely loses information."""
        assert name_tokens("dos Santos Magalhaes") == ["dos", "santos", "magalhaes"]
        assert name_tokens("dos Santos Magalhaes", drop_particles=True) == ["santos", "magalhaes"]

    def test_never_returns_empty_after_dropping_particles(self) -> None:
        """A degenerate name must not become an empty token list.

        An empty token set scores 100 against everything, which would match the
        first candidate in the pool with total confidence.
        """
        assert name_tokens("de", drop_particles=True) == ["de"]


class TestDeterministicJoins:
    def test_photo_code_join(self, bootstrap: Bootstrap) -> None:
        """The Fantasy Football Scout join - an exact integer comparison."""
        resolver = PlayerResolver(bootstrap.elements)

        result = resolver.by_photo_code(100001)

        assert result.element_id == 1
        assert result.method is ResolutionMethod.PHOTO_CODE
        assert result.is_deterministic

    def test_unknown_code_falls_through(self, bootstrap: Bootstrap) -> None:
        resolver = PlayerResolver(bootstrap.elements)

        assert resolver.by_photo_code(999_999).resolved is False

    def test_temporary_codes_are_excluded(self, bootstrap: Bootstrap) -> None:
        """`has_temporary_code` means the code is a placeholder.

        Joining on it would confidently attach one player's predicted lineup slot
        to another player's stats - a wrong answer that looks entirely plausible.
        """
        resolver = PlayerResolver(bootstrap.elements)

        assert resolver.by_photo_code(100007).resolved is False

    def test_elements_by_code_excludes_temporary(self, bootstrap: Bootstrap) -> None:
        by_code = bootstrap.elements_by_code()

        assert 100001 in by_code
        assert 100007 not in by_code


class TestCollisions:
    def test_the_collision_exists_in_the_fixture(self, bootstrap: Bootstrap) -> None:
        """Guards the premise rather than restating it in a comment."""
        collisions = find_web_name_collisions(bootstrap.elements)

        assert "Wilson" in collisions
        assert len(collisions["Wilson"]) == 2

    def test_team_constraint_disambiguates(self, bootstrap: Bootstrap) -> None:
        """Team first, then position. Team is the far stronger constraint."""
        resolver = PlayerResolver(
            bootstrap.elements,
            element_types={1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"},
        )

        newcastle = resolver.resolve("Callum Wilson", source="test", team_id=2)
        fulham = resolver.resolve("Harry Wilson", source="test", team_id=3)

        assert newcastle.element_id == 2
        assert fulham.element_id == 3

    def test_refuses_a_bare_surname_without_a_team(self, bootstrap: Bootstrap) -> None:
        """A tie must be rejected outright, not resolved by coin flip.

        With two Wilsons in the pool a 95 that ties another 95 carries no
        information, and turning it into a recommendation would be worse than
        admitting we do not know.
        """
        resolver = PlayerResolver(bootstrap.elements)

        result = resolver.resolve("Wilson", source="test")

        assert result.resolved is False
        assert "margin" in result.detail.lower() or "risky" in result.detail.lower()


class TestFuzzyMatching:
    def test_matches_the_legal_surname_chain(self, bootstrap: Bootstrap) -> None:
        """`token_set_ratio` is the right scorer here.

        Plain `ratio` is sunk by the surplus tokens in "dos Santos Magalhaes";
        the set comparison ignores them, which is exactly the distortion we face.
        """
        resolver = PlayerResolver(bootstrap.elements)

        result = resolver.resolve("Gabriel Magalhaes", source="test", team_id=1)

        assert result.element_id == 5

    def test_matches_an_initialised_name(self, bootstrap: Bootstrap) -> None:
        resolver = PlayerResolver(bootstrap.elements)

        result = resolver.resolve("Jurrien Timber", source="test", team_id=1)

        assert result.element_id == 4

    def test_matches_across_the_apostrophe_variants(self, bootstrap: Bootstrap) -> None:
        resolver = PlayerResolver(bootstrap.elements)

        result = resolver.resolve("Nico O’Reilly", source="test", team_id=1)

        assert result.element_id == 10

    def test_rejects_a_low_score(self, bootstrap: Bootstrap) -> None:
        resolver = PlayerResolver(bootstrap.elements)

        result = resolver.resolve("Completely Different Person", source="test", team_id=1)

        assert result.resolved is False
        assert result.element_id is None

    def test_unresolved_names_are_collected_for_the_report(self, bootstrap: Bootstrap) -> None:
        """Unmatched players go in the caveats rather than being silently dropped."""
        resolver = PlayerResolver(bootstrap.elements)
        resolver.resolve("Nobody At All", source="test", team_id=1)

        assert len(resolver.unresolved) == 1
        assert "Nobody At All" in resolver.unresolved[0]


class TestAliasCache:
    def test_cache_short_circuits_the_cascade(self, bootstrap: Bootstrap) -> None:
        """Each player is fuzzy-matched once, ever.

        This is what keeps the fuzzy layer's blast radius small - a bad match is
        made once and can be corrected once.
        """
        resolver = PlayerResolver(bootstrap.elements, aliases={"pi-123": 5})

        result = resolver.resolve("Anything At All", source="premierinjuries", source_id="pi-123")

        assert result.element_id == 5
        assert result.method is ResolutionMethod.CACHE

    def test_new_resolutions_are_queued_for_persistence(self, bootstrap: Bootstrap) -> None:
        resolver = PlayerResolver(bootstrap.elements)

        resolver.resolve("Gabriel Magalhaes", source="understat", source_id="u-77", team_id=1)

        assert "u-77" in resolver.newly_resolved
        element_id, _method, score = resolver.newly_resolved["u-77"]
        assert element_id == 5
        assert score > 0

    def test_stale_cache_entry_is_ignored(self, bootstrap: Bootstrap) -> None:
        """A cached id no longer in bootstrap must not be returned."""
        resolver = PlayerResolver(bootstrap.elements, aliases={"pi-999": 99_999})

        result = resolver.resolve(
            "Gabriel Magalhaes", source="premierinjuries", source_id="pi-999", team_id=1
        )

        assert result.element_id == 5, "should fall through to matching, not return a dead id"


class TestScoring:
    def test_known_name_is_considered(self, bootstrap: Bootstrap) -> None:
        element = next(e for e in bootstrap.elements if e.id == 5)

        assert score_against("Gabriel", element) > 80

    def test_score_is_zero_for_an_empty_name(self, bootstrap: Bootstrap) -> None:
        element = bootstrap.elements[0]

        assert score_against("", element) == 0.0
