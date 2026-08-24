"""Tests for the hand-rolled ACP stdio JSON-RPC connection and runtime adapter."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from weftmark.adapters.acp import AcpConnection
from weftmark.application.ports.runtime import RuntimeAdapterError, RuntimeErrorCode

_ECHO_AGENT = """
import json
import sys

def _write(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "ping":
        _write({"jsonrpc": "2.0", "id": message["id"], "result": {"pong": message["params"]["value"]}})
    elif message["method"] == "boom":
        _write({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "boom failed"}})
"""


@pytest.fixture
def echo_agent(tmp_path: Path) -> subprocess.Popen[bytes]:
    script = tmp_path / "echo_agent.py"
    script.write_text(textwrap.dedent(_ECHO_AGENT), encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    yield process
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)


def test_request_returns_matching_result(echo_agent: subprocess.Popen[bytes]) -> None:
    connection = AcpConnection(echo_agent, request_handlers={}, on_notification=lambda method, params: None)
    try:
        result = connection.request("ping", {"value": 42})
    finally:
        connection.close()
    assert result == {"pong": 42}


def test_request_raises_on_agent_error(echo_agent: subprocess.Popen[bytes]) -> None:
    connection = AcpConnection(echo_agent, request_handlers={}, on_notification=lambda method, params: None)
    try:
        with pytest.raises(RuntimeAdapterError) as excinfo:
            connection.request("boom", {})
    finally:
        connection.close()
    assert excinfo.value.code is RuntimeErrorCode.RUNTIME_FAILED
    assert "boom failed" in excinfo.value.detail


def test_request_times_out_when_agent_is_silent(tmp_path: Path) -> None:
    script = tmp_path / "silent_agent.py"
    script.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    connection = AcpConnection(process, request_handlers={}, on_notification=lambda method, params: None)
    try:
        with pytest.raises(RuntimeAdapterError) as excinfo:
            connection.request("ping", {"value": 1}, timeout=0.2)
    finally:
        connection.close()
        process.kill()
        process.wait(timeout=5)
    assert excinfo.value.code is RuntimeErrorCode.TRANSPORT_FAILED
