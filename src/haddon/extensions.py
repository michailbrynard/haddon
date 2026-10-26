"""Which optional extensions this installation runs.

The journal core is the product; statements, review, artifacts and local tax records enrich it
(``docs/architecture.md``).  "Optional" has to mean something you can actually switch off.  Issue 0004's exit gate is
that the core journal works with reconciliation disabled.  So there is a switch, the transport does not publish what
the switch turns off, and the close gates do not demand evidence nobody is expected to supply.

The switch is deliberately a process-level setting rather than a per-entity column.  Turning an extension on or
off changes which commands exist, so it belongs with the other deployment configuration; a stored flag would
make the published surface vary by row, which is exactly the shape issue 0006 is trying to remove.

``HADDON_RECONCILIATION=off`` (or ``0``/``false``/``no``) disables the reconciliation extension.  Nothing is
deleted by disabling it: statements already loaded stay readable through the ordinary event and audit history.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

RECONCILIATION = "reconciliation"

#: every extension that can be switched, and the environment variable that switches it
SWITCHES = {RECONCILIATION: "HADDON_RECONCILIATION"}

_OFF = {"0", "off", "false", "no"}

#: in-process overrides, set by configuration or by a test.  ``None`` means "read the environment".
_OVERRIDES: dict[str, bool | None] = {name: None for name in SWITCHES}


def enabled(name: str = RECONCILIATION) -> bool:
    """Return whether this extension is running.  Extensions are on unless switched off."""
    if name not in SWITCHES:
        raise KeyError(f"unknown extension {name!r}")
    override = _OVERRIDES[name]
    if override is not None:
        return override
    return os.environ.get(SWITCHES[name], "").strip().lower() not in _OFF


def set_enabled(name: str, value: bool | None) -> None:
    """Override the environment for this process.  ``None`` restores the environment's answer."""
    if name not in SWITCHES:
        raise KeyError(f"unknown extension {name!r}")
    _OVERRIDES[name] = value


@contextmanager
def without(name: str = RECONCILIATION) -> Iterator[None]:
    """Run the body with this extension switched off, whatever the environment says."""
    previous = _OVERRIDES[name]
    set_enabled(name, False)
    try:
        yield
    finally:
        set_enabled(name, previous)
