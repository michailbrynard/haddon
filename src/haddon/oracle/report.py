"""What an oracle run produces: a list of named gates, each pass or fail, and a report that is ``ok`` only when
every required gate ran and passed.

The required list matters.  A run that crashes half way through records the gate it died in and has no row for
the rest, so ``ok`` cannot mean "nothing recorded a failure".  It means every name in the report's ``required``
list is present and passed, which a partial run can never satisfy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Every gate a book-level oracle run must produce.  A missing name is a failure, not a silence.
REQUIRED_GATES = ("import", "export", "counts", "totals", "monthly_totals", "canonical", "coverage",
                  "posting_memos", "round_trip", "artifacts", "baseline")

MAX_SHOWN = 12  # mismatches quoted verbatim in a gate's summary; the full count is always in `detail`


@dataclass(frozen=True)
class Gate:
    """One named check.  ``detail`` is JSON-ready and always carries the counts the gate compared, so a passing
    gate proves it looked at something rather than at nothing."""

    name: str
    ok: bool
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    mismatches: tuple[str, ...] = ()

    def line(self) -> str:
        """The gate's PASS/FAIL line, followed by its mismatches.

        Mismatches are printed whenever there are any, including on a passing gate: a gate that passes because a
        known-open difference is exactly what was pinned must still show the difference."""
        head = f"{'PASS' if self.ok else 'FAIL'}  {self.name}: {self.summary}"
        if not self.mismatches:
            return head
        shown = list(self.mismatches[:MAX_SHOWN])
        more = len(self.mismatches) - len(shown)
        return "\n".join([head, *(f"        {m}" for m in shown), *([f"        ... and {more} more"] if more else [])])

    def as_json(self) -> dict[str, Any]:
        return asdict(self) | {"mismatches": list(self.mismatches)}


def gate(name: str, ok: bool, summary: str, detail: dict[str, Any] | None = None,
         mismatches: list[str] | tuple[str, ...] = ()) -> Gate:
    return Gate(name=name, ok=bool(ok), summary=summary, detail=detail or {}, mismatches=tuple(mismatches))


@dataclass(frozen=True)
class OracleReport:
    kind: str  # 'book' or 'continuity'
    entity: str
    entity_id: str
    start: str
    end: str
    source_revision: str | None
    gates: tuple[Gate, ...]
    required: tuple[str, ...]
    facts: dict[str, Any] = field(default_factory=dict)
    published: str | None = None  # the database path the run published to, only ever set on a fully passing run

    @property
    def names(self) -> set[str]:
        return {g.name for g in self.gates}

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(n for n in self.required if n not in self.names)

    @property
    def failures(self) -> tuple[Gate, ...]:
        return tuple(g for g in self.gates if not g.ok)

    @property
    def ok(self) -> bool:
        """True only when every required gate ran and passed.  A partial run is never ``ok``."""
        return not self.missing and not self.failures

    def summary(self) -> str:
        passed = sum(1 for g in self.gates if g.ok)
        tail = f"; MISSING {', '.join(self.missing)}" if self.missing else ""
        return (f"{self.kind} oracle {self.entity} {self.start}..{self.end}: "
                f"{'PASS' if self.ok else 'FAIL'} ({passed}/{len(self.gates)} gates passed"
                f"{', ' + str(len(self.failures)) + ' failed' if self.failures else ''}{tail})")

    def text(self) -> str:
        head = [f"{self.kind} oracle: {self.entity} ({self.entity_id}) {self.start}..{self.end}"]
        if self.source_revision:
            head.append(f"source_revision: {self.source_revision}")
        for key in sorted(self.facts):
            head.append(f"{key}: {self.facts[key]}")
        return "\n".join([*head, "", *(g.line() for g in self.gates), "", self.summary()])

    def as_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "entity": self.entity, "entity_id": self.entity_id,
                "start": self.start, "end": self.end,
                "source_revision": self.source_revision, "ok": self.ok, "missing": list(self.missing),
                "facts": self.facts, "published": self.published, "gates": [g.as_json() for g in self.gates]}
