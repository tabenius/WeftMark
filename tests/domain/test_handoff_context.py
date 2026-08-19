from __future__ import annotations

import pytest

from weftmark.domain.handoff_context import (
    DEFAULT_HANDOFF_CONTEXT_VARIANT,
    HandoffContextBudget,
    HandoffContextError,
    HandoffContextSource,
    HandoffContextVariant,
    HandoffExpansionMode,
    handoff_context_budget,
)


def test_standard_is_the_default_handoff_context_variant() -> None:
    assert DEFAULT_HANDOFF_CONTEXT_VARIANT is HandoffContextVariant.STANDARD
    assert handoff_context_budget().variant is HandoffContextVariant.STANDARD


def test_default_budgets_are_monotonic_and_cost_bounded() -> None:
    compact = handoff_context_budget("compact")
    standard = handoff_context_budget("standard")
    deep = handoff_context_budget("deep")

    assert (compact.target_tokens, compact.hard_max_tokens) == (800, 1200)
    assert (standard.target_tokens, standard.hard_max_tokens) == (1600, 2500)
    assert (deep.target_tokens, deep.hard_max_tokens) == (4000, 6500)
    assert compact.target_tokens < standard.target_tokens < deep.target_tokens
    assert compact.hard_max_tokens < standard.hard_max_tokens < deep.hard_max_tokens


def test_large_history_sources_are_retrieval_only_in_every_default_variant() -> None:
    for variant in HandoffContextVariant:
        budget = handoff_context_budget(variant)
        for source in (
            HandoffContextSource.SOURCE_FILES,
            HandoffContextSource.CHAT_TRANSCRIPT,
            HandoffContextSource.TERMINAL_HISTORY,
        ):
            assert budget.mode_for(source) is HandoffExpansionMode.RETRIEVAL_ONLY


def test_compact_and_standard_defer_diffs_but_deep_allows_bounded_excerpts() -> None:
    compact = handoff_context_budget("compact")
    standard = handoff_context_budget("standard")
    deep = handoff_context_budget("deep")

    assert compact.mode_for(HandoffContextSource.DIFF) is HandoffExpansionMode.RETRIEVAL_ONLY
    assert standard.mode_for(HandoffContextSource.DIFF) is HandoffExpansionMode.RETRIEVAL_ONLY
    assert compact.focused_excerpt_tokens == 0
    assert standard.focused_excerpt_tokens == 0

    assert deep.mode_for(HandoffContextSource.DIFF) is HandoffExpansionMode.FOCUSED_EXCERPTS
    assert deep.focused_excerpt_tokens == 1400
    assert deep.focused_excerpt_tokens < deep.target_tokens


def test_mandatory_orientation_sources_are_never_optional() -> None:
    budget = handoff_context_budget()
    for source in (
        HandoffContextSource.GOAL,
        HandoffContextSource.NEXT_ACTION,
        HandoffContextSource.LINEAGE,
        HandoffContextSource.SCOPES,
        HandoffContextSource.KNOWN_FAILURES,
    ):
        assert budget.mode_for(source) is HandoffExpansionMode.AUTOMATIC


def test_budget_serialization_is_provider_neutral_and_complete() -> None:
    payload = handoff_context_budget("standard").to_dict()

    assert payload["variant"] == "standard"
    assert payload["target_tokens"] == 1600
    assert payload["hard_max_tokens"] == 2500
    assert payload["source_modes"]["chat_transcript"] == "retrieval_only"
    assert set(payload["source_modes"]) == {source.value for source in HandoffContextSource}


def test_unknown_variant_fails_closed() -> None:
    with pytest.raises(HandoffContextError, match="unknown handoff context variant"):
        handoff_context_budget("economy-plus")


def test_invalid_custom_budget_cannot_auto_inject_chat_history() -> None:
    modes = {
        source: HandoffExpansionMode.AUTOMATIC
        for source in HandoffContextSource
    }
    modes[HandoffContextSource.DIFF] = HandoffExpansionMode.RETRIEVAL_ONLY
    modes[HandoffContextSource.SOURCE_FILES] = HandoffExpansionMode.RETRIEVAL_ONLY
    modes[HandoffContextSource.TERMINAL_HISTORY] = HandoffExpansionMode.RETRIEVAL_ONLY

    with pytest.raises(HandoffContextError, match="chat_transcript"):
        HandoffContextBudget(
            variant=HandoffContextVariant.STANDARD,
            target_tokens=1000,
            hard_max_tokens=1500,
            max_evidence_summaries=4,
            max_decision_summaries=4,
            max_known_failures=4,
            max_changed_paths=10,
            focused_excerpt_tokens=0,
            source_modes=modes,
        )


def test_hard_max_cannot_be_smaller_than_target() -> None:
    modes = {
        source: HandoffExpansionMode.AUTOMATIC
        for source in HandoffContextSource
    }
    modes[HandoffContextSource.DIFF] = HandoffExpansionMode.RETRIEVAL_ONLY
    for source in (
        HandoffContextSource.SOURCE_FILES,
        HandoffContextSource.CHAT_TRANSCRIPT,
        HandoffContextSource.TERMINAL_HISTORY,
    ):
        modes[source] = HandoffExpansionMode.RETRIEVAL_ONLY

    with pytest.raises(HandoffContextError, match="hard_max_tokens"):
        HandoffContextBudget(
            variant=HandoffContextVariant.COMPACT,
            target_tokens=1200,
            hard_max_tokens=800,
            max_evidence_summaries=4,
            max_decision_summaries=4,
            max_known_failures=4,
            max_changed_paths=10,
            focused_excerpt_tokens=0,
            source_modes=modes,
        )
