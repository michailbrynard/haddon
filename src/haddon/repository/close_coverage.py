"""The reconciliation extension's close checks: is the month covered, explained and allocated in full?

These checks read ``statements``, ``statement_lines``, ``explanations`` and ``allocations``, so they live with the
extension that loads statements rather than in the journal core (issue 0005).  The core's
``repository/close_checks.py`` keeps one check about its own drafts.  ``tests/test_core_boundaries`` flags any core
module that names those tables.

The core does not import this module.  The module registers itself with the core on import
(``close_checks.register_checks``), as the draft-revision guard does (``entries.register_allocation_guard``), so
the dependency points from the extension to the core and never back.  With the extension switched off these checks
are not in the policy at all, so a close is not asked to prove that a statement nobody expected was loaded.

There are three checks:

1. ``statement_coverage``: every statement covering the month is loaded, and its printed closing balance agrees
   with our ledger for that account and commodity;
2. ``lines_explained``: every loaded statement line dated in the window is booked, linked or ignored;
3. ``nothing_partial``: nothing dated in the window is partially allocated, from either side.

One rule governs checks 2 and 3, the substrate rule.  Lines, explanations and allocations only exist under a loaded
statement.  So a cell (account, commodity, month) that no live statement covers is reported ``unevaluable`` for
checks 2 and 3.  Without the loaded lines there is nothing to find, and "I found nothing" is not "there is
nothing".  This follows from the data and is not a setting.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from haddon.repository import tables as t
from haddon.repository.close_checks import (
    FAIL,
    PASS,
    Check,
    CheckContext,
    CheckOutcome,
    Outstanding,
    combine,
    register_checks,
    unevaluable,
    uncovered,
)
from haddon.repository.close_checks import _cell

EXTENSION = "reconciliation"


def cash_pairs(ctx: CheckContext) -> list[tuple[str, str, str]]:
    """``(account_id, account_path, commodity)`` for every cash account in the chart, archived included.

    The chart's own cash accounts are the set of accounts we expect statements for.  Issue 0004 deleted a separate
    source configuration that existed only to satisfy a trigger.  Archived accounts are kept, because a bank
    account closed mid-year still owes statements for the months before it closed.
    """
    rows = ctx.conn.execute(
        sa.select(t.accounts.c.id, t.accounts.c.path, t.account_commodities.c.commodity)
        .select_from(t.accounts.join(t.account_commodities,
                                     sa.and_(t.account_commodities.c.entity_id == t.accounts.c.entity_id,
                                             t.account_commodities.c.account_id == t.accounts.c.id)))
        .where(t.accounts.c.entity_id == ctx.entity_id, t.accounts.c.is_cash == 1)
        .order_by(t.accounts.c.path, t.account_commodities.c.commodity))
    return [(r.id, r.path, r.commodity) for r in rows]


def live_statements(ctx: CheckContext, account_id: str, commodity: str) -> list[dict[str, Any]]:
    """Every statement of this account/commodity that nothing replaces, earliest period first.

    "Live" has one definition in this codebase, ``tables.live_statement_clause``, which matches the
    ``live_statements`` SQL view.  Re-deriving it by hand is how the readers drifted apart before.
    """
    rows = ctx.conn.execute(
        sa.select(t.statements.c.id, t.statements.c.period_start, t.statements.c.period_end,
                  t.statements.c.closing_micros)
        .where(t.statements.c.entity_id == ctx.entity_id, t.statements.c.account_id == account_id,
               t.statements.c.commodity == commodity, t.live_statement_clause())
        .order_by(t.statements.c.period_start, t.statements.c.id))
    return [{"id": r.id, "period_start": r.period_start, "period_end": r.period_end,
             "closing_micros": int(r.closing_micros)} for r in rows]


def _no_cash_accounts(ctx: CheckContext, what: str) -> CheckOutcome:
    """Report an entity with no cash account as ``unevaluable``, not a pass.

    Having nothing to check is not the same as having checked everything."""
    return unevaluable(
        f"could not be evaluated: this entity has no cash account with a commodity, so {what} cannot be established",
        [Outstanding("no_cash_accounts_configured",
                     f"no cash account/commodity pair is configured, so {what} cannot be established",
                     {"entity_id": ctx.entity_id})])


def _ledger_balance(ctx: CheckContext, account_id: str, commodity: str, as_of: str) -> int | None:
    """The book balance of one account/commodity at ``as_of``, or ``None`` when it cannot be computed.

    ``None`` becomes an outstanding item, never a pass.  A balance we cannot check against is exactly what the
    "closing balance agrees" requirement is there to catch.
    """
    from haddon.reports.balances import account_balance  # local: reports imports repository
    try:
        return int(account_balance(ctx.conn, ctx.entity_id, account_id=account_id, commodity=commodity,
                                   through=as_of))
    except Exception:  # noqa: BLE001 - any failure is "unknown", and unknown is not agreement
        return None


def statement_coverage(ctx: CheckContext) -> CheckOutcome:
    pairs = cash_pairs(ctx)
    if not pairs:
        return _no_cash_accounts(ctx, "statement coverage")
    items: list[Outstanding] = []
    for account_id, path, commodity in pairs:
        loaded = live_statements(ctx, account_id, commodity)
        spans = [(s["period_start"], s["period_end"]) for s in loaded]
        for span in ctx.months:
            for gap_start, gap_end in uncovered(span.start, span.end, spans):
                items.append(Outstanding(
                    "statement_not_loaded",
                    f"no statement is loaded for {path} {commodity} over {gap_start}..{gap_end}",
                    _cell(account_id, path, commodity, span, uncovered_start=gap_start, uncovered_end=gap_end)))
            for s in loaded:
                if s["period_start"] > span.end or s["period_end"] < span.start:
                    continue
                ledger = _ledger_balance(ctx, account_id, commodity, s["period_end"])
                if ledger is None:
                    items.append(Outstanding(
                        "ledger_balance_unavailable",
                        f"the ledger balance of {path} {commodity} at {s['period_end']} could not be computed",
                        _cell(account_id, path, commodity, span, statement_id=s["id"], as_of=s["period_end"])))
                elif ledger != s["closing_micros"]:
                    items.append(Outstanding(
                        "closing_balance_disagrees",
                        f"the statement closing balance of {path} {commodity} at {s['period_end']} does not "
                        f"agree with the ledger",
                        _cell(account_id, path, commodity, span, statement_id=s["id"], as_of=s["period_end"],
                              asserted_micros=str(s["closing_micros"]), ledger_micros=str(ledger),
                              difference_micros=str(ledger - s["closing_micros"]))))
    if items:
        return CheckOutcome(FAIL, "statement coverage is incomplete", tuple(items))
    return CheckOutcome(PASS, "every month of every cash account is covered by a loaded statement whose closing "
                             "balance agrees with the ledger")


# Checks 2 and 3 first ask whether the window is covered at all (the substrate rule in the module docstring).
def _uncovered_cells(ctx: CheckContext) -> list[Outstanding]:
    out: list[Outstanding] = []
    for account_id, path, commodity in cash_pairs(ctx):
        spans = [(s["period_start"], s["period_end"]) for s in live_statements(ctx, account_id, commodity)]
        for span in ctx.months:
            for gap_start, gap_end in uncovered(span.start, span.end, spans):
                out.append(Outstanding(
                    "check_unevaluable",
                    f"no statement is loaded for {path} {commodity} over {gap_start}..{gap_end}, so nothing "
                    f"can be established about the lines of that period",
                    _cell(account_id, path, commodity, span, uncovered_start=gap_start, uncovered_end=gap_end)))
    return out


def _substrate(ctx: CheckContext, what: str) -> CheckOutcome:
    if not cash_pairs(ctx):
        return _no_cash_accounts(ctx, what)
    gaps = _uncovered_cells(ctx)
    if gaps:
        return unevaluable(f"could not be evaluated for every period: {what} cannot be established where no "
                           f"statement has been loaded", gaps)
    return CheckOutcome(PASS, "every period of the window is covered by a loaded statement")


def _lines_in_window(ctx: CheckContext) -> list[Any]:
    """Every line of a live statement dated in the window, with its statement's account and commodity."""
    return list(ctx.conn.execute(
        sa.select(t.statement_lines.c.id, t.statement_lines.c.dt, t.statements.c.account_id,
                  t.statements.c.commodity, t.statement_lines.c.amount_micros,
                  t.statement_lines.c.source_key, t.statement_lines.c.description)
        .select_from(t.statement_lines.join(t.statements,
                                            sa.and_(t.statements.c.entity_id == t.statement_lines.c.entity_id,
                                                    t.statements.c.id == t.statement_lines.c.statement_id)))
        .where(t.statement_lines.c.entity_id == ctx.entity_id, t.statement_lines.c.dt >= ctx.window_start,
               t.statement_lines.c.dt <= ctx.through, t.live_statement_clause())
        .order_by(t.statement_lines.c.dt, t.statement_lines.c.id)))


def _live_explanations(ctx: CheckContext):
    """The current word on each line, through the one shared definition of "live".

    Both halves matter: nothing supersedes the explanation, and its statement is live.  A reader that checked only
    the first would count allocations the database retired when the statement under them was replaced.
    """
    return sa.select(t.explanations.c.id, t.explanations.c.line_id, t.explanations.c.kind) \
        .where(t.explanations.c.entity_id == ctx.entity_id, t.live_explanation_clause(ctx.entity_id))


def _explained_lines(ctx: CheckContext) -> dict[str, str]:
    return {r.line_id: r.kind for r in ctx.conn.execute(_live_explanations(ctx))}


def _allocated_by_line(ctx: CheckContext) -> dict[str, int]:
    """Per line, the total of live allocations against posted entries."""
    live = _live_explanations(ctx).subquery("live")
    rows = ctx.conn.execute(
        sa.select(t.allocations.c.line_id, sa.func.sum(t.allocations.c.amount_micros).label("total"))
        .select_from(t.allocations
                     .join(live, live.c.id == t.allocations.c.explanation_id)
                     .join(t.postings, sa.and_(t.postings.c.entity_id == t.allocations.c.entity_id,
                                               t.postings.c.id == t.allocations.c.posting_id))
                     .join(t.entries, sa.and_(t.entries.c.entity_id == t.postings.c.entity_id,
                                              t.entries.c.id == t.postings.c.entry_id)))
        .where(t.allocations.c.entity_id == ctx.entity_id, t.entries.c.status == "posted")
        .group_by(t.allocations.c.line_id))
    return {r.line_id: int(r.total or 0) for r in rows}


def _allocated_by_posting(ctx: CheckContext) -> dict[str, int]:
    live = _live_explanations(ctx).subquery("live")
    rows = ctx.conn.execute(
        sa.select(t.allocations.c.posting_id, sa.func.sum(t.allocations.c.amount_micros).label("total"))
        .select_from(t.allocations.join(live, live.c.id == t.allocations.c.explanation_id))
        .where(t.allocations.c.entity_id == ctx.entity_id)
        .group_by(t.allocations.c.posting_id))
    return {r.posting_id: int(r.total or 0) for r in rows}


def lines_explained(ctx: CheckContext) -> CheckOutcome:
    substrate = _substrate(ctx, "the explanation of every loaded statement line")
    if substrate.status != PASS:
        return substrate
    explained = _explained_lines(ctx)
    items = [Outstanding("statement_line_unexplained",
                         f"statement line {r.id} dated {r.dt} is neither booked, linked nor ignored",
                         {"line_id": r.id, "dt": r.dt, "month": r.dt[:7], "account_id": r.account_id,
                          "commodity": r.commodity, "amount_micros": str(r.amount_micros),
                          "source_key": r.source_key, "description": r.description})
             for r in _lines_in_window(ctx) if r.id not in explained]
    if items:
        return CheckOutcome(FAIL, "statement lines in the period are unexplained", tuple(items))
    return CheckOutcome(PASS, "every statement line in the period is booked, linked or ignored")


def _partial_lines(ctx: CheckContext) -> CheckOutcome:
    explained, allocated = _explained_lines(ctx), _allocated_by_line(ctx)
    items = []
    for r in _lines_in_window(ctx):
        kind = explained.get(r.id)
        if kind is None or kind == "ignored":
            continue  # unexplained is check 2's finding; ignored is explained in full by construction
        total = allocated.get(r.id, 0)
        if total == r.amount_micros:
            continue
        items.append(Outstanding(
            "statement_line_partially_allocated",
            f"statement line {r.id} dated {r.dt} is allocated for part of its amount only",
            {"line_id": r.id, "dt": r.dt, "month": r.dt[:7], "account_id": r.account_id,
             "commodity": r.commodity, "amount_micros": str(r.amount_micros),
             "allocated_micros": str(total), "unallocated_micros": str(r.amount_micros - total)}))
    if items:
        return CheckOutcome(FAIL, "statement lines in the period are partially allocated", tuple(items))
    return CheckOutcome(PASS, "no statement line in the period is partially allocated")


def _partial_postings(ctx: CheckContext) -> CheckOutcome:
    """Fail any posting in the window that live allocations cover only in part.

    A half-allocated payment leg is the "partially allocated" case too, and the line side cannot see it when one
    line funds two legs."""
    allocated = _allocated_by_posting(ctx)
    if not allocated:
        return CheckOutcome(PASS, "no posting in the period carries a partial allocation")
    rows = ctx.conn.execute(
        sa.select(t.postings.c.id, t.postings.c.entry_id, t.postings.c.account_id, t.postings.c.commodity,
                  t.postings.c.amount_micros, t.entries.c.dt)
        .select_from(t.postings.join(t.entries, sa.and_(t.entries.c.entity_id == t.postings.c.entity_id,
                                                        t.entries.c.id == t.postings.c.entry_id)))
        .where(t.postings.c.entity_id == ctx.entity_id, t.entries.c.status == "posted",
               t.entries.c.dt >= ctx.window_start, t.entries.c.dt <= ctx.through,
               t.postings.c.id.in_(list(allocated)))
        .order_by(t.entries.c.dt, t.postings.c.id))
    items = [Outstanding(
        "posting_partially_allocated",
        f"posting {r.id} of entry {r.entry_id} dated {r.dt} is allocated for part of its amount only",
        {"posting_id": r.id, "entry_id": r.entry_id, "dt": r.dt, "month": r.dt[:7], "account_id": r.account_id,
         "commodity": r.commodity, "amount_micros": str(r.amount_micros),
         "allocated_micros": str(int(allocated[r.id])),
         "unallocated_micros": str(r.amount_micros - int(allocated[r.id]))})
        for r in rows if int(allocated[r.id]) != r.amount_micros]
    if items:
        return CheckOutcome(FAIL, "postings in the period are partially allocated", tuple(items))
    return CheckOutcome(PASS, "no posting in the period carries a partial allocation")


def nothing_partial(ctx: CheckContext) -> CheckOutcome:
    substrate = _substrate(ctx, "the completeness of every allocation")
    if substrate.status != PASS:
        return substrate
    return combine(_partial_lines(ctx), _partial_postings(ctx))


CHECKS = (
    Check("statement_coverage", "Statement coverage",
          "Every bank statement covering the month is loaded, and its printed closing balance agrees with the "
          "ledger for that account and commodity.",
          statement_coverage),
    Check("lines_explained", "Statement lines explained",
          "Every loaded statement line dated in the period is booked, linked or ignored.",
          lines_explained),
    Check("nothing_partial", "Nothing partial",
          "Nothing dated in the period is partially allocated, from either the line's side or the posting's.",
          nothing_partial),
)

register_checks(EXTENSION, CHECKS)
