"""Split a SQL script into complete statements (trigger bodies contain ';' so naive splitting breaks)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator


def iter_statements(script: str) -> Iterator[str]:
    buf = ""
    for piece in script.split(";"):
        buf += piece + ";"
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            buf = ""
            if stmt and stmt != ";" and not _only_comments(stmt):
                yield stmt
    if buf.strip() and not _only_comments(buf):
        raise ValueError(f"incomplete trailing SQL: {buf[:120]!r}")


def _only_comments(text: str) -> bool:
    return all(line.strip() == "" or line.strip().startswith("--") or line.strip() == ";" for line in text.splitlines())
