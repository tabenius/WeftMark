from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
THREAT_MODEL = ROOT / "docs" / "security.md"


def _document() -> str:
    return THREAT_MODEL.read_text(encoding="utf-8")


def test_threat_model_covers_declared_boundaries_and_required_abuse_cases() -> None:
    document = _document()

    for boundary in (
        "agent-identity",
        "credential-access",
        "remote-forge",
        "mcp-write",
    ):
        assert boundary in document

    for threat_id in range(1, 14):
        assert f"WM-T{threat_id:02d}" in document

    for required_topic in (
        "Credential",
        "Actor",
        "Evidence",
        "review",
        "MCP stdio boundary",
        "Loopback HTTP boundary",
        "Remote forge adapters",
    ):
        assert required_topic.casefold() in document.casefold()


def test_threat_model_records_secure_defaults_and_non_guarantees() -> None:
    document = _document().casefold()

    required_limits = (
        "not cryptographic authentication",
        "tamper-evident",
        "not a sandbox",
        "there is no network listener",
        "refuses non-loopback binds",
        "forge adapters are optional and read-only",
        "implementation and passing tests are not independent review evidence",
    )
    for statement in required_limits:
        assert statement in document

    required_defaults = (
        "mcp uses stdio, starts read-only",
        "http binds only to loopback",
        "control stays disabled without a separate write",
        "forge adapters remain read-only",
        "independent review is recorded separately",
    )
    for statement in required_defaults:
        assert statement in document
