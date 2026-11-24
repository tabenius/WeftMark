#!/usr/bin/env python3
"""Validate release policy on a freshly built native-ledger evidence bundle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


def validate(bundle: dict, head: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("release head must be an exact Git SHA")
    if not isinstance(bundle, dict) or bundle.get("schema") != "weftmark.release-evidence.v0":
        raise ValueError("unsupported release evidence schema")
    if bundle.get("source_sha") != head:
        raise ValueError("release evidence source does not match release head")
    review = bundle.get("review")
    if not isinstance(review, dict) or not isinstance(review.get("review_id"), str) or not review["review_id"].strip():
        raise ValueError("native release review is missing; provision the authorized ledger before releasing")
    if review.get("change_set_id") != "alpha-release-evidence-cs":
        raise ValueError("review belongs to another Change Set")
    if review.get("head_sha") != head:
        raise ValueError("native release review is stale or for another head")
    if review.get("outcome") != "ready" or review.get("is_releasable") is not True:
        raise ValueError("native review does not authorize release")
    evidence = bundle.get("test_evidence")
    for command in ("pytest", "make_smoke"):
        result = evidence.get(command) if isinstance(evidence, dict) else None
        if not isinstance(result, dict) or type(result.get("returncode")) is not int or result["returncode"] != 0:
            raise ValueError(f"release check did not pass: {command}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    try:
        with args.bundle.open("rb") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("release bundle exceeds size limit")
        validate(json.loads(raw), args.head)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Release refused: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
