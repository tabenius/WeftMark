"""Smoke-test the bundled Textual TUI using a real pseudo-terminal.

The application may refuse an empty/non-WeftMark repo; verify that it starts
under a PTY without runtime/import/terminal errors. No release approval implied.
"""
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path


def main():
    executable = str(Path(sys.argv[1]).resolve())
    master, slave = pty.openpty()
    env = dict(os.environ, TERM="xterm-256color", COLUMNS="100", LINES="30")
    proc = subprocess.Popen([executable, "--help"], stdin=slave, stdout=slave,
                            stderr=slave, env=env, start_new_session=True)
    os.close(slave)
    chunks = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and proc.poll() is None:
        ready, _, _ = select.select([master], [], [], 0.25)
        if ready:
            try:
                chunks.append(os.read(master, 65536))
            except OSError:
                break
    if proc.poll() is None:
        proc.kill()
    code = proc.wait(timeout=3)
    os.close(master)
    output = b"".join(chunks).decode("utf-8", "replace")
    if code != 0 or "usage:" not in output.lower() or "weftmark-tui" not in output.lower():
        raise SystemExit(f"TUI PTY smoke failed (code={code}): {output[-1500:]}")
    print("WeftMark TUI entrypoint verified under pseudo-terminal")


if __name__ == "__main__":
    main()
