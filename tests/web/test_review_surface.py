from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "review"


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_surface_is_dependency_free_and_uses_local_assets() -> None:
    html = read("index.html")
    js = read("app.js")
    css = read("styles.css")

    assert 'href="styles.css"' in html
    assert 'src="app.js"' in html
    assert 'href="manifest.webmanifest"' in html
    for source in (html, js, css):
        assert "https://" not in source
        assert "http://" not in source


def test_surface_supports_live_get_and_exported_file_without_mutations() -> None:
    html = read("index.html")
    js = read("app.js")

    assert 'id="file-button"' in html
    assert 'id="file-input"' in html
    assert 'type="file"' in html
    assert 'method: "GET"' in js
    assert "loadFromFile" in js
    assert '"weftmark.kanban-projection.v0"' in js
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert f'method: "{method}"' not in js


def test_surface_exposes_reviewer_questions_without_agent_transcript() -> None:
    html = read("index.html")
    js = read("app.js")

    assert "What needs attention?" in html
    for section in (
        'detailSection("State"',
        'detailSection("Scope blockers"',
        'detailSection("Evidence"',
        'detailSection("Review"',
        'detailSection("Handoff"',
        'detailSection("Git observation"',
    ):
        assert section in js
    assert "innerHTML" not in js


def test_sample_projection_covers_attention_review_and_semantic_collision() -> None:
    payload = json.loads(read("sample-projection.json"))

    assert payload["schema"] == "weftmark.kanban-projection.v0"
    assert payload["authority"] == {
        "coordination": "weftmark",
        "projection": "read_only",
    }
    assert payload["counts"]["cards"] == len(payload["cards"])
    assert any(card["review"] is not None for card in payload["cards"])
    assert any(card["evidence"]["failed"] > 0 for card in payload["cards"])
    assert any(card["scope_collisions"] for card in payload["cards"])
    assert any("scope_collision" in card["attention"] for card in payload["cards"])


def test_mobile_and_tablet_breakpoints_and_touch_targets_are_declared() -> None:
    css = read("styles.css")

    assert "@media (max-width: 1050px)" in css
    assert "@media (max-width: 720px)" in css
    assert "min-height: 44px" in css
    assert "92dvh" in css
