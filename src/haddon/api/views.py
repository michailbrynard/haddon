"""Shaping stored rows into the documented JSON responses.

Amounts leave here through ``wire.micros`` and nowhere else, so no route can accidentally emit a number.  The
per-item lookups an object needs (an account's allowed commodities, an entry's legs) are fetched for a whole page
in one query; shaping works on rows already in hand and never makes a hidden round trip per row.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.api.wire import link, micros
from haddon.domain.canonical import parse_tags
from haddon.repository import tables as t
from haddon.reports.types import Line, RegisterLine
from haddon.statements.shape import (BalanceSheetView, IncomeStatementView, StatementGroup, StatementLine,
                                     StatementSection, income_result_label)


def commodities_by_account(conn: Connection, entity_id: str, account_ids: list[str]) -> dict[str, list[str]]:
    if not account_ids:
        return {}
    rows = conn.execute(sa.select(t.account_commodities.c.account_id, t.account_commodities.c.commodity)
                        .where(t.account_commodities.c.entity_id == entity_id,
                               t.account_commodities.c.account_id.in_(account_ids)))
    out: dict[str, list[str]] = defaultdict(list)
    for account_id, commodity in rows:
        out[account_id].append(commodity)
    return {k: sorted(v) for k, v in out.items()}


def account(row, entity_id: str, commodities: list[str]) -> dict[str, Any]:
    return {"id": row.id, "path": row.path, "type": row.type, "code": row.code, "tax_code": row.tax_code,
            "is_cash": bool(row.is_cash), "archived": bool(row.archived), "version": row.version,
            "commodities": commodities,
            "links": {"self": link(entity_id, f"/accounts/{row.id}"),
                      "register": link(entity_id, f"/accounts/{row.id}/register")}}


def account_paths(conn: Connection, entity_id: str) -> dict[str, str]:
    return {r.id: r.path for r in conn.execute(sa.select(t.accounts.c.id, t.accounts.c.path)
                                               .where(t.accounts.c.entity_id == entity_id))}


def postings_by_entry(conn: Connection, entity_id: str, entry_ids: list[str], paths: dict[str, str]) -> dict[str, list[dict]]:
    if not entry_ids:
        return {}
    rows = conn.execute(sa.select(t.postings).where(t.postings.c.entity_id == entity_id,
                                                    t.postings.c.entry_id.in_(entry_ids))
                        .order_by(t.postings.c.entry_id, t.postings.c.seq))
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r.entry_id].append({"id": r.id, "seq": r.seq, "account_id": r.account_id,
                                "account_path": paths.get(r.account_id, ""), "commodity": r.commodity,
                                "amount_micros": micros(int(r.amount_micros)), "memo": r.memo,
                                "tax_code": r.tax_code})
    return dict(out)


def entry(row, entity_id: str, postings: list[dict]) -> dict[str, Any]:
    return {"id": row.id, "dt": row.dt, "narration": row.narration,
            "status": row.status, "origin": row.origin, "version": row.version,
            # E1: the transaction note fields, as hledger models them; '' and [] mean "this entry has none"
            "code": row.code, "comment": row.comment, "tags": [list(t) for t in parse_tags(row.tags)],
            "created_at": row.created_at, "created_by": row.created_by, "postings": postings,
            "links": {"self": link(entity_id, f"/entries/{row.id}"),
                      "events": link(entity_id, f"/entries/{row.id}/events")}}


def entries_by_id(conn: Connection, entity_id: str, entry_ids: list[str],
                  paths: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Return whole entries by id, shaped, each with ``reversed_by`` naming its mirror if it has one.

    ``reversed_by`` tells a caller whether reversing is still an option: a posted entry that already has a
    mirror cannot be reversed again, so offering the choice would only ever lead to a refusal.
    """
    if not entry_ids:
        return {}
    rows = list(conn.execute(sa.select(t.entries).where(t.entries.c.entity_id == entity_id,
                                                        t.entries.c.id.in_(entry_ids))))
    postings = postings_by_entry(conn, entity_id, [r.id for r in rows], paths)
    mirrors = {r.original_id: r.mirror_id for r in
               conn.execute(sa.select(t.corrections.c.original_id, t.corrections.c.mirror_id)
                            .where(t.corrections.c.entity_id == entity_id,
                                   t.corrections.c.original_id.in_(entry_ids)))}
    return {r.id: {**entry(r, entity_id, postings.get(r.id, [])), "reversed_by": mirrors.get(r.id)}
            for r in rows}


#: What an entry is now, derived rather than stored.  Issue 0007 asks a page to say "posted", "corrected" or
#: "reversed" clearly.  There is deliberately no column for it: it follows from the correction rows that name the
#: entry, and a stored copy would be a second thing to keep true.
POSTED, CORRECTED, REVERSED = "posted", "corrected", "reversed"


def corrections_of(conn: Connection, entity_id: str, entry_id: str) -> list[dict[str, Any]]:
    """Return every correction this entry is part of, whichever end it is on.

    A reader of one entry needs the whole chain, not only the forward half.  A mirror with no way back to what it
    reversed reads as a lone negative entry, and a reviewer concludes the books are wrong.  So the query matches
    the entry against all three columns and reports, per row, which one matched (``role``).
    """
    c = t.corrections
    rows = list(conn.execute(sa.select(c).where(
        c.c.entity_id == entity_id,
        sa.or_(c.c.original_id == entry_id, c.c.mirror_id == entry_id, c.c.replacement_id == entry_id))
        .order_by(c.c.created_at, c.c.id)))
    # The reason is not on the correction row.  A reason is why somebody did something, so it lives in the
    # append-only log beside the actor and the time, with the correction's id as subject.  It has to be joined
    # back in: it is the one part of the chain a reviewer cannot reconstruct from the numbers.
    reasons = {r.subject_id: r.reason for r in conn.execute(
        sa.select(t.events.c.subject_id, t.events.c.reason)
        .where(t.events.c.entity_id == entity_id, t.events.c.subject_kind == "correction",
               t.events.c.subject_id.in_([r.id for r in rows] or [""]))
        .order_by(t.events.c.seq))} if rows else {}
    out = []
    for r in rows:
        role = ("original" if r.original_id == entry_id
                else "mirror" if r.mirror_id == entry_id else "replacement")
        links = {"original": link(entity_id, f"/entries/{r.original_id}"),
                 "mirror": link(entity_id, f"/entries/{r.mirror_id}")}
        if r.replacement_id:
            links["replacement"] = link(entity_id, f"/entries/{r.replacement_id}")
        out.append({"id": r.id, "role": role, "original_id": r.original_id, "mirror_id": r.mirror_id,
                    "replacement_id": r.replacement_id, "reason": reasons.get(r.id, ""),
                    "created_at": r.created_at, "created_by": r.created_by, "links": links})
    return out


def entry_state(row, corrections: list[dict[str, Any]]) -> str:
    """Return ``posted``, ``corrected`` or ``reversed``, or the entry's own status when it is not posted.

    ``corrected`` and ``reversed`` must stay distinct.  Both leave the original standing with a mirror beside
    it, but only a correction also posts what the entry should have said.  Told "reversed" about a corrected
    entry, a reviewer looks for a restatement that is already there; told "corrected" about a plain reversal,
    they look for one that is not.
    """
    if row.status != POSTED:
        return row.status
    mine = [c for c in corrections if c["role"] == "original"]
    if any(c["replacement_id"] for c in mine):
        return CORRECTED
    return REVERSED if mine else POSTED


def entries_for(conn: Connection, entity_id: str, entry_ids: list[str], paths: dict[str, str]) -> list[dict[str, Any]]:
    """Like ``entries_by_id``, but as a list in the order the ids were given, skipping any not found."""
    held = entries_by_id(conn, entity_id, entry_ids, paths)
    return [held[i] for i in entry_ids if i in held]


def report_line(ln: Line) -> dict[str, Any]:
    return {"account_id": ln.account_id, "account_path": ln.path, "type": ln.type, "commodity": ln.commodity,
            "amount_micros": micros(ln.amount)}


# The shaped statements (issue 0008 section B): the JSON face of ``haddon.statements.shape``.  Ordering, sign,
# grouping and subtotals are decided once, in Python, and both the PDF and the interface read the same
# arrangement, so the interface cannot disagree with the statement that was emailed.  Nothing here computes
# anything; it renames fields already computed.  Amounts leave through ``micros``, and an absent comparative
# stays ``null`` rather than becoming zero.
def _optional(amount) -> str | None:
    return None if amount is None else micros(amount)


def _shaped_line(line: StatementLine) -> dict[str, Any]:
    return {"key": line.account_id or line.label, "account_id": line.account_id, "label": line.label,
            "amount_micros": micros(line.amount), "comparative_micros": _optional(line.comparative)}


def _shaped_group(group: StatementGroup, index: int) -> dict[str, Any]:
    return {"key": group.heading or f"group-{index}", "heading": group.heading,
            "subtotal_micros": _optional(group.subtotal),
            "comparative_subtotal_micros": _optional(group.comparative_subtotal),
            "lines": [_shaped_line(ln) for ln in group.lines]}


def _shaped_section(section: StatementSection) -> dict[str, Any]:
    return {"key": section.key, "title": section.title, "total_label": section.total_label,
            "total_micros": micros(section.total), "comparative_total_micros": _optional(section.comparative_total),
            "groups": [_shaped_group(g, i) for i, g in enumerate(section.groups)]}


def shaped_income(view: IncomeStatementView) -> dict[str, Any]:
    """Shape one commodity's income statement, including the label of its own bottom line.

    The label belongs to the view, not the document.  A document with several commodities has several bottom
    lines, and one being a profit says nothing about the others.  A label taken from the leading commodity and
    printed over all of them is how a loss ends up headed "Profit for the period".
    """
    return {"commodity": view.commodity, "start": view.start, "end": view.end,
            "result_label": income_result_label(view),
            "comparative_start": view.comparative_start, "comparative_end": view.comparative_end,
            "sections": [_shaped_section(s) for s in view.sections],
            "result_micros": micros(view.result), "comparative_result_micros": _optional(view.comparative_result)}


def shaped_balance(view: BalanceSheetView) -> dict[str, Any]:
    """Shape the balance sheet, including the synthetic result row's effect (``difference``) and per-column checks.

    Publishing ``difference`` and ``checks`` makes "the two sides meet" a report fact instead of a subtraction
    each reader does for itself.  Mid-period, when equity carries a result nothing has posted to, this is the
    figure the interface would otherwise recompute, possibly wrongly.
    """
    return {"commodity": view.commodity, "as_of": view.as_of, "comparative_as_of": view.comparative_as_of,
            "sections": [_shaped_section(s) for s in view.sections],
            "total_assets_micros": micros(view.total_assets), "total_funding_micros": micros(view.total_funding),
            "difference_micros": micros(view.difference),
            "comparative_total_assets_micros": _optional(view.comparative_total_assets),
            "comparative_total_funding_micros": _optional(view.comparative_total_funding),
            "comparative_difference_micros": _optional(view.comparative_difference),
            "checks": [{"date": date, "difference_micros": micros(diff)} for date, diff in view.checks]}


def register_line(ln: RegisterLine) -> dict[str, Any]:
    return {"entry_id": ln.entry_id, "dt": ln.dt, "seq": ln.seq, "narration": ln.narration, "origin": ln.origin,
            "account_id": ln.account_id, "account_path": ln.path, "commodity": ln.commodity,
            "amount_micros": micros(ln.amount), "running_micros": micros(ln.running), "memo": ln.memo,
            "tax_code": ln.tax_code}


def event(e) -> dict[str, Any]:
    return {"seq": e.seq, "kind": e.kind, "subject_kind": e.subject_kind, "subject_id": e.subject_id,
            "before": e.before, "after": e.after, "reason": e.reason, "request_id": e.request_id,
            "created_at": e.created_at, "created_by": e.created_by}


def snapshot(row, entity_id: str) -> dict[str, Any]:
    return {"id": row.id, "snapshot_seq_of_snapshot": row.snapshot_seq, "state_hash": row.state_hash,
            "through": row.through, "manifest": json.loads(row.manifest),
            "report": json.loads(row.report), "checks": json.loads(row.checks), "created_at": row.created_at,
            "created_by": row.created_by,
            "links": {"self": link(entity_id, f"/snapshots/{row.id}")}}


def export(row, entity_id: str) -> dict[str, Any]:
    return {"id": row.id, "snapshot_id": row.snapshot_id, "operation_id": row.operation_id,
            "manifest_sha256": row.manifest_sha256, "artifacts": json.loads(row.artifacts),
            "verifier": json.loads(row.verifier), "created_at": row.created_at, "created_by": row.created_by,
            "links": {"self": link(entity_id, f"/exports/{row.id}"),
                      "snapshot": link(entity_id, f"/snapshots/{row.snapshot_id}")}}


def close_record(row, entity_id: str) -> dict[str, Any]:
    """Shape one close or reopen: what moved, why, on whose authority and against which snapshot.

    ``links.snapshot`` matters most (issue 0005, acceptance criterion 6).  The close record says a period was
    shut; the snapshot it names is the immutable state that was shut, hashed and verified in the same
    transaction.

    ``overrides`` is stored JSON, returned as stored: the waivers that let this close through despite a real
    finding, each with the check, the actor and the reason.  It is an empty list on a reopen, which waives
    nothing.
    """
    return {"id": row.id, "kind": row.kind, "through": row.through, "prior_through": row.prior_through,
            "snapshot_id": row.snapshot_id, "reason": row.reason, "overrides": json.loads(row.overrides),
            "created_at": row.created_at, "created_by": row.created_by,
            "links": {"self": link(entity_id, f"/closes/{row.id}"),
                      "snapshot": link(entity_id, f"/snapshots/{row.snapshot_id}"),
                      "events": link(entity_id, "/events")}}


# Statements and lines (issue 0004).
def statement(s, entity_id: str, paths: dict[str, str]) -> dict[str, Any]:
    """Shape one loaded statement.

    ``close_checks`` is in the links because loading a statement turns the statement-coverage gate for that
    account and month from "nothing loaded" into a real pass or fail.  A replaced statement links to its
    successor, and a replacement back to what it replaced, so the chain can be walked from either end."""
    body = s.as_json()
    links = {"self": link(entity_id, f"/statements/{s.id}"),
             "lines": link(entity_id, f"/statements/{s.id}/lines"),
             "account": link(entity_id, f"/accounts/{s.account_id}"),
             "document": link(entity_id, f"/documents/{s.document_id}"),
             "close_checks": link(entity_id, f"/close-checks?through={s.period_end}")}
    if s.replaces_id:
        links["replaces"] = link(entity_id, f"/statements/{s.replaces_id}")
    if s.replaced_by_id:
        links["replaced_by"] = link(entity_id, f"/statements/{s.replaced_by_id}")
    return {**body, "account_path": paths.get(s.account_id, s.account_id), "links": links}


def statement_line(ln, entity_id: str) -> dict[str, Any]:
    """Shape one statement line, with links answering "where did this come from?" and "what has become of it?"."""
    return {**ln.as_json(),
            "links": {"provenance": link(entity_id, f"/provenance?line_id={ln.id}"),
                      "line": link(entity_id, f"/lines/{ln.id}")}}
