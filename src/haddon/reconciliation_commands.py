"""The reconciliation extension's typed commands: the statement loop an agent performs.

Same shape as :mod:`haddon.commands` and for the same reason (issue 0006): one business action is one named
command, callers name accounts by path and amounts as exact decimal strings in whole commodity units, and
the HTTP routes are a thin face over these functions rather than a place business logic lives.  A future
agent-tool adapter calls exactly these.

It is a separate module from :mod:`haddon.commands` because of the dependency rule, not because the surface is
different: reconciliation is an extension, the journal core may not import one (``docs/architecture.md``), and
these commands read ``repository/statements.py`` and ``repository/lines.py``.  Importing the core the other way
round is allowed and is what happens here: the leg vocabulary, the account resolution and the decimal conversion
are the core's, used once.

Published only when the extension is enabled.  ``haddon.api.app`` mounts the routes over these commands behind
the same switch that keeps the extension's close checks out of the policy, so a published command is always a
callable one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from haddon.commands import LegSpec, resolve_account, resolve_legs
from haddon.domain.errors import not_found, validation
from haddon.domain.micros import Micros
from haddon.repository import lines as lines_repo
from haddon.repository import provisioning
from haddon.repository import statements as sm
from haddon.repository.audit import AuditAction, record_action
from haddon.repository.ctx import WriteCtx


@dataclass(frozen=True)
class LineSpec:
    """One normalised statement line as a caller reads it off the document.

    ``amount`` is a decimal string in whole commodity units (``"-1250.00"``) or a ``Micros``; the statement's
    declared commodity is what converts it, so a reader that emits more decimal places than the currency has is
    refused rather than rounded.
    """

    dt: str
    description: str
    amount: str | Micros
    currency: str
    reference: str | None = None
    raw: str | None = None
    source_key: str | None = None


@dataclass(frozen=True)
class PartSpec:
    """One part of a split: an existing posting and how much of the line it is.

    A posting is named by id and not by path.  It has no path, and naming its account would not identify which
    leg.  The amount is the line's own commodity, which is the only commodity a part can be in.
    """

    posting_id: str
    amount: str | Micros


@dataclass(frozen=True)
class LoadedStatement:
    statement: sm.Statement
    audit: AuditAction

    def as_json(self) -> dict[str, Any]:
        return {**self.statement.as_json(), "audit": self.audit.as_json()}


def _commodity_of(ctx: WriteCtx, code: str, path: str):
    known = provisioning.get_commodity(ctx.conn, ctx.entity_id, code)
    if known is None:
        raise validation(f"unknown commodity {code!r}", path=path, reason="commodity_unknown", value=code)
    return known


def _amount(value: str | Micros, commodity, path: str) -> Micros:
    return value if isinstance(value, Micros) else Micros.from_decimal(value, commodity=commodity, path=path)


def load_statement(ctx: WriteCtx, *, account: str, commodity: str, period_start: str, period_end: str,
                   opening: str | Micros, closing: str | Micros, lines: Sequence[LineSpec],
                   document: sm.Document, locator: str, reason: str, page: int | None = None,
                   replaces_id: str | None = None, id: str | None = None) -> LoadedStatement:
    """Load one statement: an account, a period, an opening and a closing balance, its lines and its document.

    The whole submission is refused or written; there is no half-loaded statement.  What this command adds over
    the repository call beneath it is the caller's vocabulary (an account path, decimal amounts) and the one
    audit action.  The audit action lives here rather than in the HTTP route (issue 0006), so that any other
    transport records it too.
    """
    declared = _commodity_of(ctx, commodity, "commodity")
    resolved = resolve_account(ctx, account, "account")
    read = _lines(lines)
    statement = sm.load_statement(
        ctx, account_id=resolved.id, commodity=commodity, period_start=period_start, period_end=period_end,
        opening_micros=_amount(opening, declared, "opening"), closing_micros=_amount(closing, declared, "closing"),
        lines=[sm.Line(dt=ln.dt, description=ln.description,
                       amount_micros=_amount(ln.amount, declared, f"lines[{i}].amount"), currency=ln.currency,
                       reference=ln.reference, raw=ln.raw, source_key=ln.source_key)
               for i, ln in enumerate(read)],
        document=document, locator=locator, page=page, reason=reason, replaces_id=replaces_id, id=id)
    audit = record_action(ctx, action="statement.replaced" if replaces_id else "statement.loaded",
                          subject_kind="statement", subject_id=statement.id, reason=reason,
                          result=statement.as_json(),
                          input={"account": account, "account_id": resolved.id, "commodity": commodity,
                                 "period_start": period_start, "period_end": period_end,
                                 "opening": str(opening), "closing": str(closing),
                                 "document": document.as_json(), "locator": locator, "page": page,
                                 "replaces_id": replaces_id, "line_count": len(read)})
    return LoadedStatement(statement=statement, audit=audit)


def _lines(lines: Sequence[LineSpec]) -> list[LineSpec]:
    if not isinstance(lines, (list, tuple)):
        raise validation("lines must be a list", path="lines", reason="shape")
    for i, ln in enumerate(lines):
        if not isinstance(ln, LineSpec):
            raise validation(f"lines[{i}] is not a LineSpec", path=f"lines[{i}]", reason="line_shape")
    return list(lines)


def replace_statement(ctx: WriteCtx, *, replaces_id: str, account: str, commodity: str, period_start: str,
                      period_end: str, opening: str | Micros, closing: str | Micros,
                      lines: Sequence[LineSpec], document: sm.Document, locator: str, reason: str,
                      page: int | None = None, id: str | None = None) -> LoadedStatement:
    """Restate a statement loaded wrongly: from the wrong page, the wrong account or a truncated download.

    The predecessor is kept, stays readable and is named by the replacement, and the database allows it at most
    one successor.  It is refused while the period is locked or while a line of the predecessor is still booked.
    Undo those first, because a replacement must not orphan an entry it caused.

    The parameters are spelled out rather than forwarded, even though this is ``load_statement`` with one more
    argument: a named command an agent can discover is one whose signature says what it takes (issue 0006,
    pitfall 3), and ``**kwargs`` would publish nothing.
    """
    if not isinstance(replaces_id, str) or not replaces_id.strip():
        raise validation("name the statement being replaced", path="replaces_id", reason="required")
    return load_statement(ctx, account=account, commodity=commodity, period_start=period_start,
                          period_end=period_end, opening=opening, closing=closing, lines=lines,
                          document=document, locator=locator, reason=reason, page=page,
                          replaces_id=replaces_id, id=id)


# What can be said about a statement line: book it, link it, ignore it, or re-explain it.
def _line_commodity(ctx: WriteCtx, line_id: str):
    """Return the commodity a line's amounts are in: the statement's, which is the only one a part can be in."""
    row = sm.get_line(ctx.conn, ctx.entity_id, line_id)
    if row is None:
        raise not_found("statement line", line_id)
    statement = sm.get_statement(ctx.conn, ctx.entity_id, row.statement_id)
    if statement is None:  # pragma: no cover - a line without its statement cannot exist (foreign key)
        raise not_found("statement", row.statement_id)
    return _commodity_of(ctx, statement.commodity, "commodity")


def _parts(ctx: WriteCtx, line_id: str, parts: Sequence[PartSpec]) -> list[lines_repo.Part]:
    if not isinstance(parts, (list, tuple)) or not parts:
        raise validation("at least one part is required: which posting is this line?", path="parts",
                         reason="required")
    commodity = _line_commodity(ctx, line_id)
    out = []
    for i, part in enumerate(parts):
        if not isinstance(part, PartSpec):
            raise validation(f"parts[{i}] is not a PartSpec", path=f"parts[{i}]", reason="part_shape")
        out.append(lines_repo.Part(part.posting_id, _amount(part.amount, commodity, f"parts[{i}].amount")))
    return out


def book_line(ctx: WriteCtx, *, line_id: str, legs: Sequence[LegSpec], narration: str, reason: str,
              dt: str | None = None, memo: str | None = None, tax_code: str | None = None,
              origin: str | None = None, code: str = "", comment: str = "",
              entry_id: str | None = None, explanation_id: str | None = None) -> lines_repo.Explained:
    """Post the entry this line is, and hold the line against the cash posting it creates, in one call.

    The legs are the contra side in the ordinary caller vocabulary: account paths and decimal amounts.  The
    cash leg is not supplied and cannot be: it is the line itself.
    """
    kwargs = {} if origin is None else {"origin": origin}
    return lines_repo.book_line(ctx, line_id=line_id, legs=resolve_legs(ctx, legs), narration=narration,
                                reason=reason, dt=dt, memo=memo, tax_code=tax_code, code=code, comment=comment,
                                entry_id=entry_id, explanation_id=explanation_id, **kwargs)


def link_line(ctx: WriteCtx, *, line_id: str, parts: Sequence[PartSpec], reason: str,
              explanation_id: str | None = None) -> lines_repo.Explained:
    """Say which existing posting(s) this line is, and for how much.  This posts nothing."""
    return lines_repo.link_line(ctx, line_id=line_id, parts=_parts(ctx, line_id, parts), reason=reason,
                                explanation_id=explanation_id)


def ignore_line(ctx: WriteCtx, *, line_id: str, reason: str,
                explanation_id: str | None = None) -> lines_repo.Explained:
    """Record that this line will not be booked, and why.  The reason is the whole decision."""
    return lines_repo.ignore_line(ctx, line_id=line_id, reason=reason, explanation_id=explanation_id)


def reexplain_line(ctx: WriteCtx, *, line_id: str, kind: str, reason: str,
                   legs: Sequence[LegSpec] | None = None, parts: Sequence[PartSpec] | None = None,
                   narration: str | None = None, dt: str | None = None, memo: str | None = None,
                   tax_code: str | None = None, code: str = "", comment: str = "",
                   explanation_id: str | None = None) -> lines_repo.Explained:
    """Supersede what was said about this line before, correcting or reversing what it booked, in one transaction."""
    return lines_repo.reexplain_line(
        ctx, line_id=line_id, kind=kind, reason=reason,
        legs=None if legs is None else resolve_legs(ctx, legs),
        parts=None if parts is None else _parts(ctx, line_id, parts),
        narration=narration, dt=dt, memo=memo, tax_code=tax_code, code=code, comment=comment,
        explanation_id=explanation_id)
