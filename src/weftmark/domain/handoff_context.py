"""Token-budgeted handoff materialization policy.

A durable :class:`Handoff` remains lossless. These profiles describe how much
of its addressable context a receiving worker may expand automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class HandoffContextError(ValueError):
    """Raised when a context budget/profile is internally inconsistent."""


class HandoffContextVariant(StrEnum):
    COMPACT = "compact"
    STANDARD = "standard"
    DEEP = "deep"


class HandoffContextSource(StrEnum):
    GOAL = "goal"
    NEXT_ACTION = "next_action"
    LINEAGE = "lineage"
    SCOPES = "scopes"
    KNOWN_FAILURES = "known_failures"
    EVIDENCE_SUMMARIES = "evidence_summaries"
    DECISION_SUMMARIES = "decision_summaries"
    CHANGED_PATHS = "changed_paths"
    DIFF = "diff"
    SOURCE_FILES = "source_files"
    CHAT_TRANSCRIPT = "chat_transcript"
    TERMINAL_HISTORY = "terminal_history"


class HandoffExpansionMode(StrEnum):
    AUTOMATIC = "automatic"
    FOCUSED_EXCERPTS = "focused_excerpts"
    RETRIEVAL_ONLY = "retrieval_only"


@dataclass(frozen=True, slots=True)
class HandoffContextBudget:
    """Provider-neutral limits for one handoff materialization.

    ``target_tokens`` is the normal planning ceiling. ``hard_max_tokens`` is a
    strict ceiling: a materializer must fail closed rather than deliberately
    exceed it. Token counting may use the target provider's tokenizer when one
    is available.

    Known failures deliberately have no count limit: they are readiness-critical
    mandatory context. If all mandatory context cannot fit below ``hard_max_tokens``,
    materialization must fail rather than hide a failure.
    """

    variant: HandoffContextVariant
    target_tokens: int
    hard_max_tokens: int
    max_evidence_summaries: int
    max_decision_summaries: int
    max_changed_paths: int
    focused_excerpt_tokens: int
    source_modes: Mapping[HandoffContextSource, HandoffExpansionMode]

    def __post_init__(self) -> None:
        if self.target_tokens <= 0:
            raise HandoffContextError("target_tokens must be positive")
        if self.hard_max_tokens < self.target_tokens:
            raise HandoffContextError(
                "hard_max_tokens must be greater than or equal to target_tokens"
            )
        for name in (
            "max_evidence_summaries",
            "max_decision_summaries",
            "max_changed_paths",
            "focused_excerpt_tokens",
        ):
            if getattr(self, name) < 0:
                raise HandoffContextError(f"{name} must not be negative")

        expected = set(HandoffContextSource)
        actual = set(self.source_modes)
        if actual != expected:
            missing = sorted(source.value for source in expected - actual)
            extra = sorted(str(source) for source in actual - expected)
            detail = []
            if missing:
                detail.append(f"missing: {', '.join(missing)}")
            if extra:
                detail.append(f"extra: {', '.join(extra)}")
            raise HandoffContextError(
                "source_modes must define every context source"
                + (f" ({'; '.join(detail)})" if detail else "")
            )

        for source in (
            HandoffContextSource.GOAL,
            HandoffContextSource.NEXT_ACTION,
            HandoffContextSource.LINEAGE,
            HandoffContextSource.SCOPES,
            HandoffContextSource.KNOWN_FAILURES,
        ):
            if self.source_modes[source] is not HandoffExpansionMode.AUTOMATIC:
                raise HandoffContextError(
                    f"mandatory handoff source {source.value} must be automatic"
                )

        for source in (
            HandoffContextSource.SOURCE_FILES,
            HandoffContextSource.CHAT_TRANSCRIPT,
            HandoffContextSource.TERMINAL_HISTORY,
        ):
            if self.source_modes[source] is not HandoffExpansionMode.RETRIEVAL_ONLY:
                raise HandoffContextError(
                    f"large context source {source.value} must be retrieval_only"
                )

        diff_mode = self.source_modes[HandoffContextSource.DIFF]
        if diff_mode is HandoffExpansionMode.FOCUSED_EXCERPTS:
            if self.focused_excerpt_tokens <= 0:
                raise HandoffContextError(
                    "focused diff excerpts require a positive focused_excerpt_tokens budget"
                )
        elif self.focused_excerpt_tokens != 0:
            raise HandoffContextError(
                "focused_excerpt_tokens must be zero unless diff mode is focused_excerpts"
            )

        object.__setattr__(self, "source_modes", MappingProxyType(dict(self.source_modes)))

    def mode_for(self, source: HandoffContextSource) -> HandoffExpansionMode:
        return self.source_modes[source]

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant.value,
            "target_tokens": self.target_tokens,
            "hard_max_tokens": self.hard_max_tokens,
            "limits": {
                "evidence_summaries": self.max_evidence_summaries,
                "decision_summaries": self.max_decision_summaries,
                "changed_paths": self.max_changed_paths,
                "focused_excerpt_tokens": self.focused_excerpt_tokens,
            },
            "source_modes": {
                source.value: self.source_modes[source].value
                for source in HandoffContextSource
            },
        }


def _source_modes(*, deep: bool) -> dict[HandoffContextSource, HandoffExpansionMode]:
    modes = {
        source: HandoffExpansionMode.AUTOMATIC
        for source in HandoffContextSource
    }
    modes[HandoffContextSource.DIFF] = (
        HandoffExpansionMode.FOCUSED_EXCERPTS
        if deep
        else HandoffExpansionMode.RETRIEVAL_ONLY
    )
    for source in (
        HandoffContextSource.SOURCE_FILES,
        HandoffContextSource.CHAT_TRANSCRIPT,
        HandoffContextSource.TERMINAL_HISTORY,
    ):
        modes[source] = HandoffExpansionMode.RETRIEVAL_ONLY
    return modes


DEFAULT_HANDOFF_CONTEXT_VARIANT = HandoffContextVariant.STANDARD

_HANDOFF_CONTEXT_BUDGETS: Mapping[HandoffContextVariant, HandoffContextBudget] = MappingProxyType(
    {
        HandoffContextVariant.COMPACT: HandoffContextBudget(
            variant=HandoffContextVariant.COMPACT,
            target_tokens=800,
            hard_max_tokens=1_200,
            max_evidence_summaries=6,
            max_decision_summaries=4,
            max_changed_paths=12,
            focused_excerpt_tokens=0,
            source_modes=_source_modes(deep=False),
        ),
        HandoffContextVariant.STANDARD: HandoffContextBudget(
            variant=HandoffContextVariant.STANDARD,
            target_tokens=1_600,
            hard_max_tokens=2_500,
            max_evidence_summaries=12,
            max_decision_summaries=8,
            max_changed_paths=30,
            focused_excerpt_tokens=0,
            source_modes=_source_modes(deep=False),
        ),
        HandoffContextVariant.DEEP: HandoffContextBudget(
            variant=HandoffContextVariant.DEEP,
            target_tokens=4_000,
            hard_max_tokens=6_500,
            max_evidence_summaries=30,
            max_decision_summaries=20,
            max_changed_paths=80,
            focused_excerpt_tokens=1_400,
            source_modes=_source_modes(deep=True),
        ),
    }
)


def handoff_context_budget(
    variant: HandoffContextVariant | str = DEFAULT_HANDOFF_CONTEXT_VARIANT,
) -> HandoffContextBudget:
    """Return the immutable default budget for ``variant``.

    String input is accepted at interface boundaries. Unknown variants fail
    closed rather than silently falling back to ``standard``.
    """

    try:
        normalized = (
            variant
            if isinstance(variant, HandoffContextVariant)
            else HandoffContextVariant(str(variant))
        )
    except ValueError as error:
        raise HandoffContextError(f"unknown handoff context variant: {variant}") from error
    return _HANDOFF_CONTEXT_BUDGETS[normalized]
