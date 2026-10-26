"""Collect the suite several times under different argument lists and write the node ids out as JSON.

Run as a subprocess by ``tests/test_tier_completeness.py``.  Nesting ``pytest.main`` inside a running session is not
safe in-process, but one subprocess can collect as many times as it likes: after the first collection every
test module is already imported, so the extra collections cost almost nothing.

    python tests/tier_collect.py '[[], ["-m", "not slow"], ["-m", "slow"]]' /tmp/out.json

The name deliberately does not match ``test_*.py``, so pytest does not collect this file itself.
"""

from __future__ import annotations

import json
import sys

import pytest


class Recorder:
    """Reads ``session.items`` after collection, which is *after* ``-m``/``-k``/``--ignore`` have deselected."""

    def __init__(self) -> None:
        self.ids: list[str] | None = None

    def pytest_collection_finish(self, session) -> None:
        self.ids = [item.nodeid for item in session.items]


def collect(argv: list[str]) -> list[str]:
    recorder = Recorder()
    code = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", *argv], plugins=[recorder])
    if code != pytest.ExitCode.OK or recorder.ids is None:
        raise SystemExit(f"collection failed (exit {code}) for arguments {argv!r}")
    return recorder.ids


def main() -> None:
    argvs = json.loads(sys.argv[1])
    out = [collect(argv) for argv in argvs]
    with open(sys.argv[2], "w") as fh:
        json.dump(out, fh)


if __name__ == "__main__":
    main()
