"""Close eligibility: the checks a month must pass before ``close`` will advance the lock.

The rule is that a month should only be closed when everything is provided.  The agent still closes on its own
authority.  There is no human signature or approval button, and nothing here looks at who is acting (see
``docs/design/decisions.md`` § "Authorisation").  But ``close`` refuses a month that is not complete, and the
refusal names which check failed and exactly what is outstanding.

The core policy is one check: no entry dated in the period is still a draft.  Everything else a close asks about
(whether a statement covers the month, whether its lines are explained, whether an allocation is complete) depends
on a statement having been loaded.  That is the reconciliation extension's business and lives with it
(``repository/close_coverage.py``).  The extension registers its checks with this module, and the core never
imports the extension (``docs/architecture.md``).

A switched-off extension contributes no checks, not passing ones.  With nobody expected to load a statement,
"every statement is loaded" is a check that does not apply rather than one that passed.  Which extensions were
running is written onto the close itself.

Checks fail closed.  A check answers ``pass`` only when it has positively established completeness.  Anything
else blocks: a real finding, a component it cannot yet evaluate, an exception, an unknown status.  A check that
cannot answer "is this complete?" has not answered "yes", and its refusal says so rather than inventing a specific
problem it did not find.

The policy is a literal tuple of ``Check`` records over plain functions, not a registry.  Issue 0005 replaced a
``Gate`` base class and ``register_gate`` registry that the three statically installed checks never needed.  That
also removed a failure mode, a key declared required with no implementation registered, because a literal policy
cannot be missing an implementation.  What remains of that guard is the case that can still happen: an extension
switched on whose checks were never installed in this process, which is a blocking ``unevaluable``.

``close_checks`` takes a ``Connection``, not a ``WriteCtx``, and writes nothing, so eligibility can be inspected
before a close is attempted.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.dates import parse_date
from haddon.domain.errors import CLOSE_NOT_ELIGIBLE, HaddonError, validation
from haddon import extensions
from haddon.repository import tables as t

PASS = "pass"
FAIL = "fail"
UNEVALUABLE = "unevaluable"
STATUSES = (PASS, FAIL, UNEVALUABLE)
#: Ranked so that combining component statuses is a max over this order.
_SEVERITY = {PASS: 0, FAIL: 1, UNEVALUABLE: 2}

#: Keeps a refusal readable.  Past this many items per check the rest are counted, not listed.
MAX_OUTSTANDING = 100



def _d(value: str) -> date:
    return date.fromisoformat(value)


def _next_day(value: str) -> str:
    return (_d(value) + timedelta(days=1)).isoformat()


def _prev_day(value: str) -> str:
    return (_d(value) - timedelta(days=1)).isoformat()


def uncovered(start: str, end: str, intervals: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Return the sub-ranges of the inclusive ``[start, end]`` span that the union of ``intervals`` does not cover.

    Pure.  It answers both "which days of this month have no statement loaded" and "which days have no closing
    balance asserted".  An empty result means full coverage.
    """
    clipped = sorted((max(s, start), min(e, end)) for s, e in intervals if s <= end and e >= start)
    gaps: list[tuple[str, str]] = []
    cursor = start
    for s, e in clipped:
        if s > cursor:
            gaps.append((cursor, _prev_day(s)))
        if e >= cursor:
            cursor = _next_day(e)
        if cursor > end:
            return gaps
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


@dataclass(frozen=True)
class MonthSpan:
    """One calendar month of the close window, clipped to the window (a mid-month close closes a part-month)."""

    month: str   # YYYY-MM
    start: str
    end: str

    def as_json(self) -> dict[str, str]:
        return {"month": self.month, "start": self.start, "end": self.end}


def months_between(start: str, end: str) -> tuple[MonthSpan, ...]:
    """Every calendar month intersecting ``[start, end]``, each clipped to it.

    Months that look inactive are included like any other, because a month with no rows still needs coverage."""
    out: list[MonthSpan] = []
    cursor = start
    while cursor <= end:
        d = _d(cursor)
        month_end = _prev_day((date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)).isoformat())
        span_end = min(month_end, end)
        out.append(MonthSpan(f"{d.year:04d}-{d.month:02d}", cursor, span_end))
        cursor = _next_day(span_end)
    return tuple(out)


@dataclass(frozen=True)
class Outstanding:
    """One specific thing that would have to change for a check to pass.

    ``kind`` is a stable token an agent can branch on.  ``message`` is the sentence a human reads.  ``detail``
    carries the identifying facts (account, month, commodity, ids), with every amount as an integer micros string.
    """

    kind: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "detail": dict(self.detail)}


@dataclass(frozen=True)
class CheckOutcome:
    """What a check's ``evaluate`` returns: a status, why, and the outstanding items behind it."""

    status: str
    reason: str
    outstanding: tuple[Outstanding, ...] = ()


def combine(*outcomes: CheckOutcome) -> CheckOutcome:
    """Fold a check's components: the worst status wins and every component's findings are kept.

    ``unevaluable`` deliberately outranks ``fail``.  "I could not tell" is the weaker claim of the two, and the
    caller must hear it, so a refusal never reads as a complete list of problems when it is not one.
    """
    worst = max(outcomes, key=lambda o: _SEVERITY[o.status])
    items = tuple(i for o in outcomes for i in o.outstanding)
    reasons = [o.reason for o in outcomes if o.status != PASS] or [worst.reason]
    return CheckOutcome(worst.status, "; ".join(dict.fromkeys(reasons)), items)


@dataclass(frozen=True)
class Override:
    """What a caller may say to close over a blocking check: the check's name and why, nothing else.

    There is no id to pre-create and no wildcard.  An override is matched against the checks as the server has
    just evaluated them, so it can only name a failure that is real at this moment.  That makes a blanket bypass
    impossible to express, not merely discouraged.
    """

    check: str
    reason: str

    def as_json(self) -> dict[str, Any]:
        return {"check": self.check, "reason": self.reason}


@dataclass(frozen=True)
class Waiver:
    """One override, matched to the check it names and to what that check found at the moment of the close.

    It never changes the check's reported status, and the real finding stays visible beside it.  It only stops
    that check blocking this one close.  The actor is the principal who ran the close.  It is ``None`` on a
    read-only preview, where nobody has decided anything yet.
    """

    check: str
    through: str
    reason: str
    status_at_close: str
    reason_at_close: str
    outstanding_at_close: int
    actor: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"check": self.check, "through": self.through, "reason": self.reason, "actor": self.actor,
                "status_at_close": self.status_at_close, "reason_at_close": self.reason_at_close,
                "outstanding_at_close": self.outstanding_at_close}


@dataclass(frozen=True)
class CheckResult:
    key: str
    title: str
    requirement: str
    status: str
    reason: str
    outstanding: tuple[Outstanding, ...]
    truncated: int = 0
    waiver: Waiver | None = None

    @property
    def blocking(self) -> bool:
        """Anything that is not a positive ``pass`` blocks, unless a recorded override waives this exact check."""
        return self.status != PASS and self.waiver is None

    def as_json(self) -> dict[str, Any]:
        return {"check": self.key, "title": self.title, "requirement": self.requirement, "status": self.status,
                "reason": self.reason, "blocking": self.blocking,
                "outstanding": [o.as_json() for o in self.outstanding], "outstanding_truncated": self.truncated,
                "waiver": None if self.waiver is None else self.waiver.as_json()}


@dataclass(frozen=True)
class CloseChecks:
    """The whole answer to "may this month be closed, and if not, what is outstanding?"."""

    through: str
    window_start: str
    months: tuple[MonthSpan, ...]
    prior_through: str | None
    checks: tuple[CheckResult, ...]
    #: Overrides the caller offered that no currently blocking check answers to.  Reported here rather than
    #: raised, because reading eligibility must stay total and read-only.  ``require_eligible`` does the refusing.
    unmatched_overrides: tuple[Override, ...] = ()

    @property
    def eligible(self) -> bool:
        """True only when every required check positively passed.  Overrides do not make a month eligible."""
        return all(g.status == PASS for g in self.checks)

    @property
    def may_close(self) -> bool:
        """True when nothing blocks: every check passed, or an override waives the ones that did not.

        An override that names no current failure does not make this false.  That is a separate refusal
        (``require_eligible``), because "the month is blocked" and "you told me to override something that is
        not blocked" are different things to tell a caller.
        """
        return not any(g.blocking for g in self.checks)

    @property
    def blocking(self) -> tuple[CheckResult, ...]:
        return tuple(g for g in self.checks if g.blocking)

    def check(self, key: str) -> CheckResult:
        for g in self.checks:
            if g.key == key:
                return g
        raise KeyError(key)

    def as_json(self) -> dict[str, Any]:
        return {"through": self.through, "window_start": self.window_start, "prior_through": self.prior_through,
                "months": [m.as_json() for m in self.months], "eligible": self.eligible, "may_close": self.may_close,
                "checks": [g.as_json() for g in self.checks],
                "blocking_checks": [g.key for g in self.blocking],
                "unmatched_overrides": [o.as_json() for o in self.unmatched_overrides]}


@dataclass(frozen=True)
class CheckContext:
    """Everything a check may read.  It holds a connection, not a ``WriteCtx``, so a check cannot write."""

    conn: Connection
    entity_id: str
    through: str
    window_start: str
    months: tuple[MonthSpan, ...]


@dataclass(frozen=True)
class Check:
    """One close check: a key, a title, what it requires, and the function that answers.

    A plain record over a plain function, which replaced a base class and a registry in issue 0005.  The module
    docstring says why.
    """

    key: str
    title: str
    requirement: str
    evaluate: Callable[[CheckContext], CheckOutcome]


def _cell(account_id: str, path: str, commodity: str, span: MonthSpan, **extra: Any) -> dict[str, Any]:
    return {"account_id": account_id, "account_path": path, "commodity": commodity, "month": span.month,
            "period_start": span.start, "period_end": span.end, **extra}


def unevaluable(reason: str, items: Sequence[Outstanding] = ()) -> CheckOutcome:
    """The outcome for a check that cannot establish completeness.

    Never a pass, and never dressed up as a specific finding.  Its items say what could not be established."""
    return CheckOutcome(UNEVALUABLE, reason, tuple(items))


def _nothing_in_draft(ctx: CheckContext) -> CheckOutcome:
    """Fail any entry in the window that is still a draft, since a draft has not reached the books.

    This is the whole of the journal core's close policy.  It is always evaluable, because it depends on no
    extension and on nothing having been loaded.
    """
    rows = ctx.conn.execute(
        sa.select(t.entries.c.id, t.entries.c.dt, t.entries.c.narration, t.entries.c.origin)
        .where(t.entries.c.entity_id == ctx.entity_id, t.entries.c.status == "draft",
               t.entries.c.dt >= ctx.window_start, t.entries.c.dt <= ctx.through)
        .order_by(t.entries.c.dt, t.entries.c.id))
    items = [Outstanding("entry_not_posted",
                         f"entry {r.id} dated {r.dt} is still a draft",
                         {"entry_id": r.id, "dt": r.dt, "month": r.dt[:7], "narration": r.narration,
                          "origin": r.origin})
             for r in rows]
    if items:
        return CheckOutcome(FAIL, "entries in the period are still drafts", tuple(items))
    return CheckOutcome(PASS, "no entry in the period is still a draft")


CORE_CHECKS = (
    Check("nothing_in_draft", "Nothing in draft", "No entry dated in the period is still a draft.",
          _nothing_in_draft),
)

#: Set by the reconciliation extension at import (``close_coverage``), the same way the journal core learns what
#: an allocation is (``entries.register_allocation_guard``).  The core does not import the extension.  The
#: extension tells the core it is here.
_EXTENSION_CHECKS: dict[str, tuple[Check, ...]] = {}


def register_checks(extension: str, checks: Sequence[Check]) -> None:
    _EXTENSION_CHECKS[extension] = tuple(checks)


def _not_installed(extension: str) -> Check:
    """A blocking check for an extension that is switched on but never registered its checks.

    This replaces the old guard against "a key in ``REQUIRED_GATE_KEYS`` with no registered implementation".  An
    installation running reconciliation whose coverage checks were never imported would otherwise close on the
    core check alone, which is a vacuous pass.
    """
    return Check(f"{extension}_checks", f"{extension} checks", f"required when the {extension} extension is on",
                 lambda ctx: unevaluable(
                     f"could not be evaluated: the {extension} extension is enabled but its close checks are not "
                     f"installed in this process",
                     [Outstanding("extension_checks_not_installed",
                                  f"the {extension} extension is running but registered no close checks",
                                  {"extension": extension})]))


def policy() -> tuple[Check, ...]:
    """The checks a close runs in this process: the core's, plus those of every extension that is switched on.

    A disabled extension contributes no checks, not passing ones.  With nobody expected to load a statement, a
    check that statements are loaded does not apply.  Which extensions were running is recorded on the close
    itself (``checks_for_audit``), so a close read years later still says what was and was not asked.
    """
    checks = CORE_CHECKS
    for extension in sorted(extensions.SWITCHES):
        if not extensions.enabled(extension):
            continue
        registered = _EXTENSION_CHECKS.get(extension)
        checks += registered if registered else (_not_installed(extension),)
    return checks


def _window(conn: Connection, entity_id: str, *, through: str, prior_through: str | None) -> tuple[str, tuple[MonthSpan, ...]]:
    """The span this close newly closes: the day after the lock, through ``through``.

    With no lock yet, it starts on the earliest ``draft`` or ``posted`` entry, the states a check can have a
    finding about.  A first close covers everything the journal holds, which makes it the expensive one.

    Starting on the earliest posted entry instead would leave a blind spot.  The lock covers every date on or
    before ``through``, so a draft dated before the earliest posted entry would be locked by a close whose
    ``nothing_in_draft`` never looked at it, and the close record would claim no draft where there was one.  The
    window is the only lower bound every check shares, so this is where it is fixed (issue 0005 review, finding 1;
    ``docs/design/decisions.md``).

    ``void`` entries are left out on purpose.  No check has a finding to make about one, and counting it would
    drag the window back for good.  Void an entry dated years before trading began, and every first close after
    that would owe statement coverage for the empty years between.
    """
    if prior_through is not None:
        start = _next_day(prior_through)
    else:
        earliest = conn.execute(sa.select(sa.func.min(t.entries.c.dt))
                                .where(t.entries.c.entity_id == entity_id,
                                       t.entries.c.status.in_(("draft", "posted")))).scalar()
        if earliest is None:
            raise validation("this entity holds no draft or posted entry, so no period can be closed",
                             path="through", reason="no_entries")
        start = str(earliest)
    if start > through:
        raise validation(f"close must advance the lock beyond {prior_through}", path="through", reason="not_advancing",
                         current_through=prior_through, through=through)
    return start, months_between(start, through)


def evaluate(ctx: CheckContext, checks: Sequence[Check]) -> tuple[CheckResult, ...]:
    """Run every check and fold the results fail-closed.

    A check can fail to say "pass" in three ways, and all of them block.  It returns ``fail`` or ``unevaluable``.
    It raises, including a ``HaddonError``, since a check that cannot read what it needs has not established
    completeness.  Or it returns a status it is not allowed to.  An extension that is enabled but never
    registered its checks is handled by ``policy``.
    """
    results: list[CheckResult] = []
    for check in checks:
        try:
            outcome = check.evaluate(ctx)
        except Exception as exc:  # noqa: BLE001 - a check that raised did not establish completeness
            outcome = unevaluable(f"could not be evaluated: the check raised {type(exc).__name__}: {exc}",
                                  [Outstanding("check_raised", f"the check raised {type(exc).__name__}",
                                               {"check": check.key, "error": str(exc)})])
        if not isinstance(outcome, CheckOutcome) or outcome.status not in STATUSES:
            got = getattr(outcome, "status", outcome)
            outcome = unevaluable(f"could not be evaluated: the check returned {got!r}, which is not one of {STATUSES}",
                                  [Outstanding("check_malformed", "the check returned a result that is not a status",
                                               {"check": check.key, "returned": repr(got)})])
        items = outcome.outstanding
        results.append(CheckResult(check.key, check.title or check.key, check.requirement, outcome.status,
                                  outcome.reason, items[:MAX_OUTSTANDING], max(0, len(items) - MAX_OUTSTANDING)))
    return tuple(sorted(results, key=lambda r: r.key))


def close_checks(conn: Connection, entity_id: str, *, through: str, overrides: Sequence[Override] = (),
                 actor: str | None = None, checks: Sequence[Check] | None = None) -> CloseChecks:
    """Evaluate eligibility for a close through ``through``.  This only reads.

    Each of ``overrides`` is a check name and a reason in the caller's words, matched here against the checks as
    they have just been evaluated.  An override never rewrites a check's status.  The real finding stays visible
    and the waiver sits beside it.  An override that names no currently blocking check is returned in
    ``unmatched_overrides`` rather than raised, so reading eligibility stays total.  ``require_eligible`` turns it
    into a refusal.
    """
    from haddon.repository.closing import get_lock  # local: closing imports this module
    parse_date(through, path="through")
    lock = get_lock(conn, entity_id)
    window_start, months = _window(conn, entity_id, through=through, prior_through=lock.through)
    cctx = CheckContext(conn=conn, entity_id=entity_id, through=through, window_start=window_start, months=months)
    results = evaluate(cctx, policy() if checks is None else checks)
    results, unmatched = apply_overrides(results, through=through, overrides=overrides, actor=actor)
    return CloseChecks(through=through, window_start=window_start, months=months, prior_through=lock.through,
                       checks=results, unmatched_overrides=unmatched)


def check_overrides(overrides: Sequence[Override], *, known: Sequence[str] | None = None) -> tuple[Override, ...]:
    """Validate the shape of what a caller offered, before any check has been looked at.

    The main mistake this guards against is a bare string.  ``overrides="statement_coverage"`` would otherwise
    iterate into single characters and quietly waive nothing.
    """
    if isinstance(overrides, (str, bytes)) or not isinstance(overrides, (list, tuple)):
        raise validation("overrides is a list of {check, reason} objects", path="overrides", reason="list_required")
    known = {c.key for c in policy()} if known is None else set(known)
    seen: set[str] = set()
    out: list[Override] = []
    for index, override in enumerate(overrides):
        path = f"overrides[{index}]"
        if not isinstance(override, Override):
            raise validation("an override names a check and says why", path=path, reason="override_shape")
        if override.check not in known:
            raise validation(f"unknown close check {override.check!r}", path=f"{path}.check", reason="check_unknown",
                             value=override.check, known=sorted(known))
        if not isinstance(override.reason, str) or not override.reason.strip():
            raise validation("an override must say why", path=f"{path}.reason", reason="required")
        if override.check in seen:
            raise validation(f"check {override.check!r} is overridden twice", path=f"{path}.check",
                             reason="override_duplicate", value=override.check)
        seen.add(override.check)
        out.append(override)
    return tuple(out)


def apply_overrides(results: Sequence[CheckResult], *, through: str, overrides: Sequence[Override],
                    actor: str | None) -> tuple[tuple[CheckResult, ...], tuple[Override, ...]]:
    """Attach each override to the blocking check it names.  Return the results and the overrides that matched none.

    The waiver records what the check found at this moment, not when somebody first noticed the problem.  An
    override is evaluated against a fresh reading of the books every time, so it cannot carry a stale
    justification forward into a later close.
    """
    checked = check_overrides(overrides)
    by_check = {o.check: o for o in checked}
    out: list[CheckResult] = []
    matched: set[str] = set()
    for r in results:
        override = by_check.get(r.key)
        if override is None or not r.blocking:
            out.append(r)
            continue
        matched.add(r.key)
        waiver = Waiver(check=r.key, through=through, reason=override.reason, actor=actor,
                        status_at_close=r.status, reason_at_close=r.reason,
                        outstanding_at_close=len(r.outstanding) + r.truncated)
        out.append(CheckResult(r.key, r.title, r.requirement, r.status, r.reason, r.outstanding, r.truncated, waiver))
    return tuple(out), tuple(o for o in checked if o.check not in matched)


def require_eligible(conn: Connection, entity_id: str, *, through: str, overrides: Sequence[Override] = (),
                     actor: str | None = None, checks_override: Sequence[Check] | None = None) -> CloseChecks:
    """Apply the precondition ``close`` needs.  Return the evaluated checks, or raise.

    There are two refusals, deliberately kept apart.  409 ``close_not_eligible`` has one detail per blocking
    check.  422 is for an override that names no current failure.  An override must answer a problem that
    exists, so an override list assembled once and replayed at every close stops working as soon as its check
    goes green.
    """
    checks = close_checks(conn, entity_id, through=through, overrides=overrides, actor=actor,
                          checks=checks_override)
    if checks.unmatched_overrides:
        names = ", ".join(o.check for o in checks.unmatched_overrides)
        raise validation(
            f"these overrides name no check that is blocking this close: {names}. An override answers a current "
            f"failure; re-read the checks and override only what is actually blocking.",
            path="overrides", reason="override_not_blocking",
            checks=[o.check for o in checks.unmatched_overrides], through=through)
    if checks.may_close:
        return checks
    blocking = checks.blocking
    names = ", ".join(g.key for g in blocking)
    raise HaddonError(
        CLOSE_NOT_ELIGIBLE,
        f"the period through {through} is not complete: {names}. Provide what is outstanding, or pass an "
        f"override naming each blocking check and why it is being closed over.",
        details=[{**g.as_json(), "path": "through", "through": through} for g in blocking])


def checks_for_audit(checks: CloseChecks) -> dict[str, Any]:
    """Build the JSON recorded on the audit trail with every close.

    It says what was evaluated, what it found, and what was waived, by whom and why.  It is kept compact enough
    for an event row."""
    body = checks.as_json()
    body["outstanding_counts"] = {g.key: len(g.outstanding) + g.truncated for g in checks.checks}
    body["waived_checks"] = {g.key: g.waiver.as_json() for g in checks.checks if g.waiver is not None}
    # A disabled extension contributes no checks, so without this a close read years later could not tell
    # "every check passed" from "there were fewer checks".
    body["extensions"] = {name: extensions.enabled(name) for name in sorted(extensions.SWITCHES)}
    return json.loads(json.dumps(body, sort_keys=True))
