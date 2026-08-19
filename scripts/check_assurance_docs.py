#!/usr/bin/env python3
"""Validate machine-readable WeftMark assurance facts against public claims."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FACTS_PATH = ROOT / "assurance" / "facts.json"
README_PATH = ROOT / "README.md"
BEGIN = "<!-- assurance:begin -->"
END = "<!-- assurance:end -->"
STATE_KEYS = ("planned", "implemented", "verified", "reviewed", "releasable")
VERIFY_KINDS = {"test", "ci"}
REVIEW_KINDS = {"review", "security"}
RELEASE_KINDS = {"release", "deployment"}


class AssuranceError(ValueError):
    pass


def load_facts() -> dict[str, Any]:
    try:
        data = json.loads(FACTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssuranceError(f"cannot load {FACTS_PATH.relative_to(ROOT)}: {exc}") from exc
    if data.get("schema") != "weftmark.assurance-facts.v0":
        raise AssuranceError("facts schema must be weftmark.assurance-facts.v0")
    return data


def _ref_path(ref: str) -> Path | None:
    if "://" in ref or ref.startswith("github:"):
        return None
    relative = ref.split("#", 1)[0]
    return ROOT / relative


def validate_facts(data: dict[str, Any]) -> list[dict[str, Any]]:
    facts = data.get("facts")
    if not isinstance(facts, list) or len(facts) < 3:
        raise AssuranceError("facts must contain at least three capability records")
    seen: set[str] = set()
    for fact in facts:
        if not isinstance(fact, dict):
            raise AssuranceError("every assurance fact must be an object")
        fact_id = fact.get("id")
        label = fact.get("label")
        if not isinstance(fact_id, str) or not fact_id.strip():
            raise AssuranceError("assurance fact id must be non-empty")
        if fact_id in seen:
            raise AssuranceError(f"duplicate assurance fact id: {fact_id}")
        seen.add(fact_id)
        if not isinstance(label, str) or not label.strip():
            raise AssuranceError(f"{fact_id}: label must be non-empty")
        for key in STATE_KEYS:
            if not isinstance(fact.get(key), bool):
                raise AssuranceError(f"{fact_id}: {key} must be boolean")
        evidence = fact.get("evidence")
        if not isinstance(evidence, list):
            raise AssuranceError(f"{fact_id}: evidence must be a list")
        kinds: set[str] = set()
        for item in evidence:
            if not isinstance(item, dict):
                raise AssuranceError(f"{fact_id}: malformed evidence record")
            kind = item.get("kind")
            ref = item.get("ref")
            if not isinstance(kind, str) or not isinstance(ref, str) or not ref.strip():
                raise AssuranceError(f"{fact_id}: evidence requires kind and ref")
            kinds.add(kind)
            local_path = _ref_path(ref)
            if local_path is not None and not local_path.exists():
                raise AssuranceError(
                    f"{fact_id}: evidence reference does not exist: {ref}"
                )

        if fact["implemented"] and not fact["planned"]:
            raise AssuranceError(f"{fact_id}: implemented capability must be planned")
        if fact["verified"]:
            if not fact["implemented"]:
                raise AssuranceError(f"{fact_id}: verified requires implemented")
            if not (kinds & VERIFY_KINDS):
                raise AssuranceError(
                    f"{fact_id}: verified requires explicit test or CI evidence"
                )
        if fact["reviewed"]:
            if not fact["implemented"]:
                raise AssuranceError(f"{fact_id}: reviewed requires implemented")
            if not (kinds & REVIEW_KINDS):
                raise AssuranceError(
                    f"{fact_id}: reviewed requires explicit review/security evidence"
                )
        if fact["releasable"]:
            if not (fact["implemented"] and fact["verified"] and fact["reviewed"]):
                raise AssuranceError(
                    f"{fact_id}: releasable requires implemented, verified, and reviewed"
                )
            if not (kinds & RELEASE_KINDS):
                raise AssuranceError(
                    f"{fact_id}: releasable requires release/deployment evidence"
                )
    return facts


def _resolve_path(value: Any, dotted: str) -> Any:
    current = value
    for part in dotted.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise AssuranceError(f"invalid list path component {part!r} in {dotted}") from exc
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise AssuranceError(f"claim path not found: {dotted}")
    return current


def validate_claims(data: dict[str, Any]) -> int:
    claims = data.get("claims", [])
    if not isinstance(claims, list):
        raise AssuranceError("claims must be a list")
    cache: dict[Path, Any] = {}
    for claim in claims:
        if not isinstance(claim, dict):
            raise AssuranceError("claim must be an object")
        target = claim.get("target")
        dotted = claim.get("path")
        if not isinstance(target, str) or not isinstance(dotted, str):
            raise AssuranceError("claim requires target and path")
        path = ROOT / target
        if path not in cache:
            try:
                cache[path] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AssuranceError(f"cannot read claim target {target}: {exc}") from exc
        actual = _resolve_path(cache[path], dotted)
        if "equals" in claim and actual != claim["equals"]:
            raise AssuranceError(
                f"{target}:{dotted} expected {claim['equals']!r}, got {actual!r}"
            )
        if "contains" in claim:
            needle = claim["contains"]
            if not isinstance(actual, str) or not isinstance(needle, str) or needle not in actual:
                raise AssuranceError(
                    f"{target}:{dotted} does not contain expected text {needle!r}"
                )
        if "equals" not in claim and "contains" not in claim:
            raise AssuranceError(f"{target}:{dotted} claim needs equals or contains")
    return len(claims)


def _mark(value: bool) -> str:
    return "yes" if value else "—"


def render_readme_block(facts: list[dict[str, Any]]) -> str:
    lines = [
        BEGIN,
        "### Assurance snapshot",
        "",
        "This table is generated from `assurance/facts.json`; `implemented` is not",
        "treated as `verified`, and nothing is marked releasable without explicit",
        "release evidence.",
        "",
        "| Capability | Implemented | Verified | Reviewed | Releasable |",
        "| --- | --- | --- | --- | --- |",
    ]
    for fact in facts:
        lines.append(
            "| "
            + " | ".join(
                [
                    fact["label"],
                    _mark(fact["implemented"]),
                    _mark(fact["verified"]),
                    _mark(fact["reviewed"]),
                    _mark(fact["releasable"]),
                ]
            )
            + " |"
        )
    lines.extend(["", END])
    return "\n".join(lines)


def _replace_block(text: str, block: str) -> str:
    if BEGIN not in text or END not in text:
        raise AssuranceError("README assurance markers are missing")
    before, rest = text.split(BEGIN, 1)
    _, after = rest.split(END, 1)
    return before + block + after


def validate_or_write_readme(facts: list[dict[str, Any]], *, write: bool) -> None:
    try:
        current = README_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssuranceError(f"cannot read README.md: {exc}") from exc
    expected = render_readme_block(facts)
    if BEGIN not in current or END not in current:
        if not write:
            raise AssuranceError("README assurance block is missing; run with --write")
        anchor = "\n## From Frog to WeftMark\n"
        if anchor not in current:
            raise AssuranceError("README insertion anchor not found")
        current = current.replace(anchor, "\n" + expected + "\n" + anchor, 1)
        README_PATH.write_text(current, encoding="utf-8")
        return
    updated = _replace_block(current, expected)
    if updated != current:
        if not write:
            raise AssuranceError("README assurance block is stale; run with --write")
        README_PATH.write_text(updated, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the generated README assurance block",
    )
    args = parser.parse_args()
    try:
        data = load_facts()
        facts = validate_facts(data)
        claim_count = validate_claims(data)
        validate_or_write_readme(facts, write=args.write)
    except AssuranceError as exc:
        print(f"assurance check: {exc}")
        return 1
    print(f"assurance check: {len(facts)} facts and {claim_count} claims valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
