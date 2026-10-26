"""The typed application commands: the journal loop an agent or a reviewer actually performs.

One command is one business action.  Each writes its domain rows and exactly one immutable audit action in the
caller's transaction, and creates no proposal, decision or application row (issue 0003).  The audit action is the
record of authority, not a synthetic approval chain.  The HTTP transport and any future agent-tool adapter call
these functions, so the business logic lives here once.

* ``initialize_entity`` provisions a generic entity, its commodities and a small starter chart.  There is no corpus
  checkout, no statement configuration and no caller-supplied calendar book.  The journal is one continuous ledger
  (issue 0002), so a date is all a write needs.
* ``create_account``, ``archive_account`` and ``restore_account`` change the chart, each with its own audit action.
* ``post_entry`` writes a balanced entry from account paths and exact amounts, on the acting principal's own
  authority, and records one audit action.
* ``post_batch`` is the same write, N at a time, applied whole or refused whole against the count and per-commodity
  totals the caller asserts.  It posts through ``post_entry``: one posting path, no exemptions.
* ``correct_entry`` and ``reverse_entry`` disagree with a posted entry the only way a posted entry is ever disagreed
  with.  A mirror stands beside the original, plus one audit action naming why.
* ``close_period`` and ``reopen_period`` shut a period, or deliberately reopen one.  A close evaluates its own
  checks, computes and stores its own verification snapshot, moves the scalar lock and writes one audit action, all
  in the caller's transaction.  No caller ever supplies a snapshot or any other internal id (issue 0005).  It passes
  a through-date, a reason, the lock version it expects and, where a check is blocking, an override naming that
  check and saying why.

Amounts are exact ``Micros`` values or decimal strings in whole commodity units at this layer.  That same compact
vocabulary is published by the journal-command transport; storage and query projections continue to expose
canonical integer micros.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from haddon.domain.canonical import check_tags
from haddon.domain.errors import ACCOUNT_UNKNOWN, UNBALANCED, HaddonError, not_found, validation
from haddon.domain.micros import Commodity, Micros
from haddon.repository import closing as closing_repo
from haddon.repository import corrections as corrections_repo
from haddon.repository import entries as entries_repo
from haddon.repository import provisioning
from haddon.repository import requests as requests_repo
from haddon.repository.close_checks import CloseChecks, Override
from haddon.repository.audit import AuditAction, record_action
from haddon.repository.ctx import WriteCtx
from haddon.repository.entries import Entry, Leg, Posting, check_open, validate_header, validate_legs

DEFAULT_ORIGIN = "manual"


@dataclass(frozen=True)
class LegSpec:
    """One leg as a caller knows it: an account path (or id) and an exact amount.

    ``amount`` is a decimal string in whole units (``"12.34"``, ``"-250"``) or a ``Micros``; a bare ``int`` or
    ``float`` is refused.  A decimal string is converted using the commodity's declared precision, exactly and
    never through binary floating point.
    """

    account: str
    commodity: str
    amount: str | Micros
    memo: str | None = None
    tax_code: str | None = None


@dataclass(frozen=True)
class Replacement:
    """A corrected entry's replacement, in the same caller vocabulary as ``LegSpec``."""

    dt: str
    narration: str
    origin: str
    legs: Sequence[LegSpec]
    code: str = ""
    comment: str = ""
    tags: tuple = ()


def resolve_account(ctx: WriteCtx, account: str, path: str):
    if not isinstance(account, str) or not account.strip():
        raise validation("an account path is required", path=path, reason="required")
    found = provisioning.account_by_path(ctx.conn, ctx.entity_id, account)
    if found is None:
        found = provisioning.get_account(ctx.conn, ctx.entity_id, account)
    if found is None:
        raise HaddonError(ACCOUNT_UNKNOWN, f"no account {account!r} in this entity",
                          details=[{"path": path, "reason": "account_unknown", "account": account, "id": account}])
    return found


def _resolve_amount(ctx: WriteCtx, value: str | Micros, commodity: str, path: str) -> Micros:
    if isinstance(value, Micros):
        return value
    known = provisioning.get_commodity(ctx.conn, ctx.entity_id, commodity)
    if known is None:
        raise validation(f"unknown commodity {commodity!r}", path=f"{path}.commodity", reason="commodity_unknown",
                         value=commodity)
    return Micros.from_decimal(value, commodity=known, path=f"{path}.amount")


def _reason_or(reason: str | None, default: str) -> str:
    """Return the caller's reason if it gave one, otherwise the command's own default sentence."""
    return reason.strip() if isinstance(reason, str) and reason.strip() else default


def resolve_legs(ctx: WriteCtx, legs: Sequence[LegSpec]) -> list[Leg]:
    if not isinstance(legs, (list, tuple)) or not legs:
        raise validation("legs are required", path="legs", reason="required")
    out: list[Leg] = []
    for i, leg in enumerate(legs):
        if not isinstance(leg, LegSpec):
            raise validation(f"legs[{i}] is not a LegSpec", path=f"legs[{i}]", reason="leg_shape")
        account = resolve_account(ctx, leg.account, f"legs[{i}].account")
        amount = _resolve_amount(ctx, leg.amount, leg.commodity, f"legs[{i}]")
        out.append(Leg(account.id, leg.commodity, amount, leg.memo, leg.tax_code))
    return out

#: a small, generic starter chart: what an ordinary company holds, owes, spends and earns.  Every account allows
#: the base currency, and a caller may pass a different chart or extend this one with ``create_account``.
STARTER_CHART = (
    ("assets:cash", "asset", True),
    ("assets:receivables", "asset", False),
    ("liabilities:payables", "liability", False),
    ("expenses:general", "expense", False),
    ("income:sales", "income", False),
)
#: the equity account an opening balance or a retained result lands in.  A continuous journal does not fold a
#: year's result into it with a carry entry (issue 0002); the P&L accounts simply keep their history.
RETAINED_PATH = "equity:retained-earnings"


@dataclass(frozen=True)
class InitializedEntity:
    entity: provisioning.Entity
    commodities: dict[str, Commodity]
    accounts: dict[str, provisioning.Account]
    audit: AuditAction

    def as_json(self) -> dict[str, Any]:
        return {"entity_id": self.entity.id, "name": self.entity.name, "base_currency": self.entity.base_currency,
                "commodities": [{"code": c.code, "precision": c.precision} for c in self.commodities.values()],
                "accounts": [{"id": a.id, "path": a.path, "type": a.type} for a in self.accounts.values()],
                "audit": self.audit.as_json()}


@dataclass(frozen=True)
class PostedEntry:
    entry: Entry
    audit: AuditAction

    def as_json(self) -> dict[str, Any]:
        return {"entry_id": self.entry.id, "status": self.entry.status, "dt": self.entry.dt,
                "narration": self.entry.narration, "origin": self.entry.origin, "version": self.entry.version,
                "legs": [_leg_json(p) for p in self.entry.legs], "audit": self.audit.as_json()}


@dataclass(frozen=True)
class PostedCorrection:
    correction: corrections_repo.Correction
    audit: AuditAction

    def as_json(self) -> dict[str, Any]:
        return {"correction_id": self.correction.id, "original_id": self.correction.original_id,
                "mirror_id": self.correction.mirror_id, "replacement_id": self.correction.replacement_id,
                "audit": self.audit.as_json()}


def _leg_json(p: Posting | Leg) -> dict[str, Any]:
    amount = p.amount.value if hasattr(p.amount, "value") else int(p.amount)
    return {"account_id": p.account_id, "commodity": p.commodity, "amount_micros": str(amount),
            "memo": getattr(p, "memo", None), "tax_code": getattr(p, "tax_code", None)}


def initialize_entity(conn, *, actor: str, id: str, name: str, legal_form: str, country: str, base_currency: str,
                      base_precision: int,
                      commodities: Sequence[tuple[str, int]] = (), chart: Sequence[tuple[str, str, bool]] = STARTER_CHART,
                      tax_regime: dict | None = None, now: str | None = None, reason: str | None = None,
                      request_id: str | None = None) -> InitializedEntity:
    """Provision a generic entity, its commodities and a starter chart, in the caller's transaction.

    The base currency precision is explicit; guessing two decimals would silently corrupt a currency such as JPY.
    Additional commodities are declared beside it.  An equity account for opening balances and retained results is
    added; no book or year is named anywhere, because the journal is continuous (issue 0002).
    """
    declared = {base_currency: base_precision}
    for index, (code, precision) in enumerate(commodities):
        if code in declared:
            raise validation(f"commodity {code!r} is declared more than once",
                             path=f"commodities[{index}]", reason="duplicate", value=code)
        declared[code] = precision
    ctx = provisioning.provision_entity(conn, actor=actor, id=id, name=name, legal_form=legal_form, country=country,
                                        base_currency=base_currency, tax_regime=tax_regime, now=now,
                                        request_id=request_id)
    found: dict[str, Commodity] = {}
    for code, precision in declared.items():
        found[code] = provisioning.add_commodity(ctx, code=code, precision=precision)
    accounts: dict[str, provisioning.Account] = {}
    for path, type_, is_cash in chart:
        accounts[path] = provisioning.add_account(ctx, path=path, type=type_, is_cash=is_cash,
                                                  commodities=[base_currency])
    entity = provisioning.read_entity(conn)
    accounts[RETAINED_PATH] = provisioning.add_account(ctx, path=RETAINED_PATH, type="equity",
                                                       is_cash=False, commodities=[base_currency])
    audit = record_action(ctx, action="entity.initialized", subject_kind="entity", subject_id=id,
                          reason=_reason_or(reason, "initialize entity"),
                          result={"entity_id": id, "name": name, "base_currency": base_currency,
                                  "commodities": sorted(declared), "accounts": sorted(accounts)},
                          input={"id": id, "name": name, "legal_form": legal_form, "country": country,
                                 "base_currency": base_currency, "base_precision": base_precision,
                                 "commodities": [{"code": code, "precision": precision}
                                                 for code, precision in sorted(declared.items())],
                                 "chart": [{"path": path, "type": type_, "is_cash": is_cash}
                                           for path, type_, is_cash in chart],
                                 "tax_regime": tax_regime})
    return InitializedEntity(entity=entity, commodities=found, accounts=accounts, audit=audit)


@dataclass(frozen=True)
class ChartChange:
    """One account written or retired, and the audit action that recorded it."""

    account: provisioning.Account
    audit: AuditAction

    def as_json(self) -> dict[str, Any]:
        return {"account_id": self.account.id, "path": self.account.path, "type": self.account.type,
                "archived": self.account.archived, "version": self.account.version,
                "audit": self.audit.as_json()}


#: what an account's audit action records when the caller gives no words of its own.  The chart is structure
#: rather than a booking, so "an account was added" is the whole content of the act; a caller with more to say
#: passes ``reason`` and it is kept verbatim, like every other reason in this ledger.
ADDED_TO_THE_CHART = "add an account to the chart"


def create_account(ctx: WriteCtx, *, path: str, type: str, commodities: Sequence[str], is_cash: bool = False,
                   code: str | None = None, tax_code: str | None = None, id: str | None = None,
                   reason: str | None = None) -> ChartChange:
    """Add one account to the chart, and record that somebody did.

    An account written here is permanent and every future report carries it, so it gets an audit action like
    every other write (issue 0006).  Before that, the only trace of who had extended the chart was ``created_by``
    on the row.

    ``reason`` is optional, the one deliberate difference from a booking.  Adding an account is structure rather
    than an assertion about what happened, so it follows ``initialize_entity``: the action names itself when the
    caller has nothing to add.  ``archive_account`` does require a reason, because retiring an account is a
    decision about the books.
    """
    account = provisioning.add_account(ctx, path=path, type=type, is_cash=is_cash, commodities=list(commodities),
                                       id=id, code=code, tax_code=tax_code)
    audit = record_action(ctx, action="account.created", subject_kind="account", subject_id=account.id,
                          reason=_reason_or(reason, ADDED_TO_THE_CHART),
                          result={"account_id": account.id, "path": account.path, "type": account.type,
                                  "is_cash": account.is_cash, "code": account.code,
                                  "tax_code": account.tax_code, "commodities": sorted(dict.fromkeys(commodities)),
                                  "version": account.version},
                          input={"path": path, "type": type, "is_cash": is_cash,
                                 "commodities": list(commodities), "code": code, "tax_code": tax_code, "id": id,
                                 "reason": reason})
    return ChartChange(account=account, audit=audit)


def archive_account(ctx: WriteCtx, *, account: str, reason: str, expected_version: int) -> ChartChange:
    """Retire an account: nothing may be posted to it again, and everything already posted to it stays.

    A reason is required.  Archiving is the only way an account leaves the chart, it is visible in every later
    report as an account nothing moves on, and "why did this stop being used?" is exactly the question a
    reviewer asks about it years later.

    ``expected_version`` is the same concurrency answer ``close_period`` gives: two callers retiring an account
    from two readings of the chart is a race, and the second one fails rather than acting on a chart it never
    saw.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise validation("archiving an account needs a reason: it is why nothing may be posted to it again",
                         path="reason", reason="required")
    found = resolve_account(ctx, account, "account")
    retired = provisioning.archive_account(ctx, account_id=found.id, expected_version=expected_version)
    audit = record_action(ctx, action="account.archived", subject_kind="account", subject_id=retired.id,
                          reason=reason,
                          result={"account_id": retired.id, "path": retired.path, "archived": retired.archived,
                                  "version": retired.version},
                          input={"account": account, "reason": reason, "expected_version": expected_version})
    return ChartChange(account=retired, audit=audit)


def restore_account(ctx: WriteCtx, *, account: str, reason: str, expected_version: int) -> ChartChange:
    """Put a retired account back in use, and record why it had to come back.

    Archiving is a decision, not a fact about the books, and every other decision this ledger records can be
    taken back by another one: ``reopen_period`` moves a closed period's lock backwards, a correction restates a
    posted entry, ``replace_statement`` withdraws a statement loaded wrongly.  Without this, retiring an account
    would be a trap.  An account archived while it still carried a balance, a posted entry or an unbooked
    statement line could not be reversed against, booked to, or closed over, by any published command
    (``decisions.md``, "Nothing a command can reach is a place commands cannot leave").

    Nothing about the books moves: this is the same provisioning write archiving is, in the other direction, and
    the account's history was never hidden in the first place.  What comes back is the ability to post to it.
    The ``reason`` is what a reviewer reads to understand a chart that shows an account retired and then not.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise validation("restoring an account needs a reason: it is why it is in use again",
                         path="reason", reason="required")
    found = resolve_account(ctx, account, "account")
    restored = provisioning.restore_account(ctx, account_id=found.id, expected_version=expected_version)
    audit = record_action(ctx, action="account.restored", subject_kind="account", subject_id=restored.id,
                          reason=reason,
                          result={"account_id": restored.id, "path": restored.path, "archived": restored.archived,
                                  "version": restored.version},
                          input={"account": account, "reason": reason, "expected_version": expected_version})
    return ChartChange(account=restored, audit=audit)

def _entry_input(dt: str, narration: str, reason: str, origin: str, legs: Sequence[Leg], *, id: str | None,
                 code: str, comment: str, tags: Sequence[tuple[str, str]]) -> dict[str, Any]:
    return {"dt": dt, "narration": narration, "reason": reason, "origin": origin,
            "id": id, "code": code, "comment": comment, "tags": [list(tag) for tag in tags],
            "legs": [{"account_id": leg.account_id, "commodity": leg.commodity,
                      "amount_micros": leg.amount.render() if hasattr(leg.amount, "render") else str(leg.amount),
                      "memo": leg.memo, "tax_code": leg.tax_code} for leg in legs]}


def post_entry(ctx: WriteCtx, *, dt: str, narration: str, reason: str, legs: Sequence[LegSpec],
               origin: str = DEFAULT_ORIGIN, code: str = "", comment: str = "", tags: Iterable[Any] = (),
               id: str | None = None) -> PostedEntry:
    """Write one balanced entry directly into the books and record one audit action.

    A reason is required: an entry records why it was written, not only what it moved.  Legs name account paths
    (or ids) and exact amounts.  The command reuses the ordinary posting path (draft, then seal), so it meets the
    balance, two-leg, commodity, precision, lock and account rules exactly as every other booking does; there is
    still one way a posting reaches the store.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise validation("reason is required: an entry records why it was written, not only what it moved",
                         path="reason", reason="required")
    if not isinstance(narration, str) or not narration.strip():
        raise validation("narration is required", path="narration", reason="required")
    normalized_tags = check_tags(tags)
    resolved = resolve_legs(ctx, legs)
    validate_legs(ctx, resolved)
    validate_header(ctx, dt, narration, origin, allow_reserved=False, code=code, comment=comment,
                    tags=normalized_tags)
    check_open(ctx.conn, dt)
    entry = entries_repo.post_entry(ctx, dt=dt, narration=narration, origin=origin, legs=resolved, id=id, code=code,
                                    comment=comment, tags=normalized_tags)
    audit = record_action(ctx, action="entry.posted", subject_kind="entry", subject_id=entry.id, reason=reason,
                          result={"entry_id": entry.id, "status": entry.status, "dt": entry.dt,
                                  "narration": entry.narration, "origin": entry.origin, "version": entry.version,
                                  "legs": [_leg_json(p) for p in entry.legs]},
                          input=_entry_input(dt, narration, reason, origin, resolved, id=id, code=code,
                                             comment=comment, tags=normalized_tags))
    return PostedEntry(entry=entry, audit=audit)


#: the most entries one batch may carry.  A batch is applied in a single write transaction, and the write
#: transaction is exclusive (``BEGIN IMMEDIATE``), so its size is how long every other writer waits.  The number
#: is published in ``/capabilities``'s ``limits`` and refused here, so the advertised maximum and the enforced
#: one are the same number; a caller with more than this splits the work into several batches, each of which is
#: refused or applied whole on its own.
MAX_BATCH_ENTRIES = 1000


@dataclass(frozen=True)
class BatchEntrySpec:
    """One entry of a batch, in exactly the vocabulary ``post_entry`` already takes.

    ``reason`` is the one difference and it is optional: a batch has a reason of its own, and repeating it on
    every entry of a year's conversion would be a thousand copies of one sentence.  An entry that has something
    more specific to say carries its own and it is recorded verbatim, like every other reason here.
    """

    dt: str
    narration: str
    legs: Sequence[LegSpec]
    reason: str | None = None
    origin: str | None = None
    code: str = ""
    comment: str = ""
    tags: tuple = ()
    id: str | None = None


@dataclass(frozen=True)
class BatchTotalSpec:
    """What the caller says one commodity's side of the batch adds up to, in micros.

    Both sides are non-negative magnitudes: ``debits`` is every positive posting of that commodity and
    ``credits`` every negative one.  Each entry balances per commodity, so the two are necessarily equal once
    the batch is built, which is the point of asking for both.  They are computed by the caller from its own
    source and compared against what this ledger actually posted, so a figure transposed on one side of the
    conversion disagrees with the other and the batch is refused.
    """

    commodity: str
    debits: str | Micros
    credits: str | Micros


@dataclass(frozen=True)
class PostedBatch:
    """One batch: the operation that records it, the entries it wrote, and the totals it was checked against."""

    operation_id: str
    entries: tuple[Entry, ...]
    totals: dict[str, tuple[Micros, Micros]]
    audit: AuditAction

    @property
    def entry_ids(self) -> list[str]:
        return [entry.id for entry in self.entries]

    def as_json(self) -> dict[str, Any]:
        return {"operation_id": self.operation_id, "count": len(self.entries), "entry_ids": self.entry_ids,
                "totals": totals_json(self.totals), "audit": self.audit.as_json()}


def totals_json(totals: dict[str, tuple[Micros, Micros]]) -> list[dict[str, str]]:
    return [{"commodity": code, "debits_micros": debits.render(), "credits_micros": credits.render()}
            for code, (debits, credits) in sorted(totals.items())]


def _asserted_totals(specs: Sequence[BatchTotalSpec]) -> dict[str, tuple[Micros, Micros]]:
    """Parse the caller's declared totals.

    A commodity named twice is refused rather than merged.  Two different claims about one commodity mean the
    caller does not know what it is sending.
    """
    if not isinstance(specs, (list, tuple)):
        raise validation("asserted.totals is a list of per-commodity totals", path="asserted.totals",
                         reason="list_required")
    declared: dict[str, tuple[Micros, Micros]] = {}
    for i, spec in enumerate(specs):
        path = f"asserted.totals[{i}]"
        if not isinstance(spec, BatchTotalSpec):
            raise validation(f"{path} is not a BatchTotalSpec", path=path, reason="shape")
        if not isinstance(spec.commodity, str) or not spec.commodity.strip():
            raise validation("a commodity is required", path=f"{path}.commodity", reason="required")
        if spec.commodity in declared:
            raise validation(f"commodity {spec.commodity!r} is asserted more than once", path=f"{path}.commodity",
                             reason="duplicate", value=spec.commodity)
        sides = []
        for side, value in (("debits", spec.debits), ("credits", spec.credits)):
            amount = value if isinstance(value, Micros) else Micros.parse(value, path=f"{path}.{side}_micros")
            if amount.value < 0:
                raise validation(f"{side} is a magnitude, so it is never negative", path=f"{path}.{side}_micros",
                                 reason="negative", value=amount.render())
            sides.append(amount)
        declared[spec.commodity] = (sides[0], sides[1])
    return declared


def _actual_totals(entries: Sequence[Entry]) -> dict[str, tuple[Micros, Micros]]:
    """Total what was actually posted, from the postings the store now holds rather than from the submission."""
    totals: dict[str, tuple[Micros, Micros]] = {}
    for entry in entries:
        for posting in entry.legs:
            debits, credits = totals.get(posting.commodity, (Micros(0), Micros(0)))
            if posting.amount.value >= 0:
                debits = debits + posting.amount
            else:
                credits = credits - posting.amount
            totals[posting.commodity] = (debits, credits)
    return totals


def _check_totals(declared: dict[str, tuple[Micros, Micros]], actual: dict[str, tuple[Micros, Micros]]) -> None:
    """Refuse the batch when any side of any commodity disagrees, naming every disagreement at once.

    Every commodity of both sets is compared, so a commodity the batch posted and the caller did not declare is
    a mismatch as loudly as a wrong figure: a conversion that quietly dropped a currency is exactly the failure
    an asserted total exists to catch, and a check over the declared commodities alone would not see it.
    """
    mismatches = []
    for code in sorted(set(declared) | set(actual)):
        want = declared.get(code)
        got = actual.get(code)
        if want == got:
            continue
        detail: dict[str, Any] = {"path": "asserted.totals", "reason": "batch_totals_do_not_match",
                                  "commodity": code}
        detail["asserted_debits_micros"] = want[0].render() if want else None
        detail["asserted_credits_micros"] = want[1].render() if want else None
        detail["actual_debits_micros"] = got[0].render() if got else None
        detail["actual_credits_micros"] = got[1].render() if got else None
        mismatches.append(detail)
    if mismatches:
        raise HaddonError(UNBALANCED,
                          f"the batch does not match its asserted totals: "
                          f"{', '.join(d['commodity'] for d in mismatches)}",
                          details=mismatches)


def _short(text: Any, limit: int = 80) -> str:
    """Truncate a narration so it reads well in a refusal message."""
    rendered = text if isinstance(text, str) else repr(text)
    return rendered if len(rendered) <= limit else rendered[:limit - 3] + "..."


def _at_entry(exc: HaddonError, index: int, spec: BatchEntrySpec) -> HaddonError:
    """Re-raise a per-entry refusal with the batch position of the entry it is about.

    Every per-entry rule here is ``post_entry``'s, and every refusal it raises is phrased for the caller who
    sent that one entry: the path is ``legs[0].account``, ``dt``, ``id``.  That is exact for one entry and no
    use at all for nine hundred.  A conversion told "no account 'expenses:nope' in this entity" and nothing else
    would have to bisect its own submission to find out where.

    So the code, the message and the existing details are kept, and a caller matching on ``account_unknown`` or
    on ``details[0].remedy`` still matches.  The entry's place is added to all of them: the index every ``path``
    is now relative to, and enough of the entry (id, date, narration) to find the row in the source it was
    converted from.  The batch is still refused whole; this only says where to look.
    """
    where: dict[str, Any] = {"batch_index": index, "entry_dt": spec.dt,
                             "entry_narration": _short(spec.narration)}
    if spec.id is not None:
        where["entry_id"] = spec.id
    details = [{**detail, **where,
                **({"path": f"entries[{index}].{detail['path']}"} if isinstance(detail.get("path"), str) else {})}
               for detail in exc.details] or [{**where, "path": f"entries[{index}]"}]
    return HaddonError(exc.code, f"entries[{index}] ({spec.dt} {_short(spec.narration)!r}): {exc.message}",
                       details=details, retryable=exc.retryable, status=exc.status)


def post_batch(ctx: WriteCtx, *, reason: str, entries: Sequence[BatchEntrySpec], asserted_count: int,
               asserted_totals: Sequence[BatchTotalSpec], id: str | None = None) -> PostedBatch:
    """Post N entries as one unit, refused whole when it is not the batch the caller said it was.

    The ledger's own writes are one entry at a time, which is the right shape for a reviewer and the wrong one
    for a conversion: an agent loading a year of a converted journal through a loop of single posts has no way
    to tell afterwards that it loaded all of it, and neither has anybody reading the books.  A dropped entry, a
    retried one that posted twice, a connection that died two thirds of the way through: each leaves a set of
    books that is internally consistent and simply wrong, and nothing in the ledger disagrees with it.

    So a batch carries the caller's own assertion of what it contains (how many entries, and what each
    commodity's debits and credits come to), and that assertion is checked against what was actually posted
    before anything commits.  It is the same shape ``load_statement`` has had since chunk F1: the ledger does
    not know what the source said, so the caller declares it and the ledger refuses the submission that does not
    match.  A batch is applied in one transaction, so a refusal for any reason leaves nothing behind at all,
    whether it is a broken assertion, a locked period or one unknown account in entry 900.

    Every per-entry rule is ``post_entry``'s, because every entry is posted by ``post_entry``.  There is still
    one posting path, and a batch buys no exemption from the balance rule, the account and precision rules or
    the period lock.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise validation("a batch records why it was loaded, not only what it moved", path="reason",
                         reason="required")
    if not isinstance(entries, (list, tuple)) or not entries:
        raise validation("a batch needs at least one entry", path="entries", reason="required")
    if len(entries) > MAX_BATCH_ENTRIES:
        raise validation(f"a batch carries at most {MAX_BATCH_ENTRIES} entries and this one carries "
                         f"{len(entries)}; split it into several, each refused or applied whole on its own",
                         path="entries", reason="batch_too_large", value=len(entries))
    if isinstance(asserted_count, bool) or not isinstance(asserted_count, int):
        raise validation("asserted.count is the number of entries this batch contains", path="asserted.count",
                         reason="int_required")
    # Checked before anything is posted: the caller and this ledger disagree about the size of the submission
    # itself, which says nothing about the books and everything about the request that arrived.
    if asserted_count != len(entries):
        raise validation(f"the batch asserts {asserted_count} entries and carries {len(entries)}",
                         path="asserted.count", reason="batch_count_does_not_match", value=asserted_count)
    declared = _asserted_totals(asserted_totals)

    intent = {"kind": "batch", "count": asserted_count, "totals": totals_json(declared), "reason": reason}
    operation = requests_repo.create_operation(ctx, kind="batch", intent=intent, id=id)
    operation = requests_repo.transition_operation(ctx, operation.id, expected_version=operation.version,
                                                   state="running")
    posted: list[Entry] = []
    for i, spec in enumerate(entries):
        if not isinstance(spec, BatchEntrySpec):
            raise validation(f"entries[{i}] is not a BatchEntrySpec", path=f"entries[{i}]", reason="shape")
        try:
            written = post_entry(ctx, dt=spec.dt, narration=spec.narration,
                                 reason=_reason_or(spec.reason, reason), legs=spec.legs,
                                 origin=DEFAULT_ORIGIN if spec.origin is None else spec.origin,
                                 code=spec.code, comment=spec.comment, tags=spec.tags, id=spec.id)
        except HaddonError as exc:
            raise _at_entry(exc, i, spec) from exc
        posted.append(written.entry)
    actual = _actual_totals(posted)
    _check_totals(declared, actual)

    counts = {"entries": len(posted), "postings": sum(len(entry.legs) for entry in posted),
              "commodities": sorted(actual)}
    result = {"entry_ids": [entry.id for entry in posted], "totals": totals_json(actual)}
    requests_repo.transition_operation(ctx, operation.id, expected_version=operation.version, state="succeeded",
                                       counts=counts, result=result)
    audit = record_action(ctx, action="batch.posted", subject_kind="operation", subject_id=operation.id,
                          reason=reason, result={"operation_id": operation.id, **counts, **result},
                          input={"reason": reason, "count": asserted_count, "totals": totals_json(declared),
                                 "entries": [_entry_input(spec.dt, spec.narration,
                                                          _reason_or(spec.reason, reason),
                                                          DEFAULT_ORIGIN if spec.origin is None else spec.origin,
                                                          entry.legs, id=spec.id, code=spec.code,
                                                          comment=spec.comment, tags=entry.tags)
                                             for spec, entry in zip(entries, posted)]})
    return PostedBatch(operation_id=operation.id, entries=tuple(posted), totals=actual, audit=audit)


def _resolve_replacement(ctx: WriteCtx, replacement: Replacement | None) -> corrections_repo.Replacement | None:
    if replacement is None:
        return None
    if not isinstance(replacement, Replacement):
        raise validation("replacement must be a Replacement", path="replacement", reason="shape")
    return corrections_repo.Replacement(dt=replacement.dt, narration=replacement.narration,
                                        origin=replacement.origin, legs=resolve_legs(ctx, replacement.legs),
                                        code=replacement.code, comment=replacement.comment, tags=replacement.tags)


def _replacement_input(replacement: corrections_repo.Replacement | None) -> dict[str, Any] | None:
    if replacement is None:
        return None
    return {"dt": replacement.dt, "narration": replacement.narration, "origin": replacement.origin,
            "code": replacement.code, "comment": replacement.comment,
            "tags": [list(tag) for tag in replacement.tags],
            "legs": [{"account_id": leg.account_id, "commodity": leg.commodity,
                      "amount_micros": leg.amount.render() if hasattr(leg.amount, "render") else str(leg.amount),
                      "memo": leg.memo, "tax_code": leg.tax_code} for leg in replacement.legs]}


def _undo(ctx: WriteCtx, *, action: str, original_id: str, dt: str, reason: str,
          replacement: Replacement | None, narration: str | None, entry_id: str | None,
          mirror_id: str | None, replacement_id: str | None) -> PostedCorrection:
    resolved = _resolve_replacement(ctx, replacement)
    correction = corrections_repo.reverse_entry(
        ctx, original_id=original_id, dt=dt, reason=reason, narration=narration, replacement=resolved,
        id=entry_id, mirror_id=mirror_id, replacement_id=replacement_id)
    # A statement line booked to the original follows it onto the replacement in this same transaction, or the
    # line would be left pointing at an entry the books no longer stand behind.
    followed = corrections_repo.follow_correction(ctx, correction, reason)
    result = {"correction_id": correction.id, "original_id": correction.original_id,
              "mirror_id": correction.mirror_id, "replacement_id": correction.replacement_id}
    if followed:
        result["explanations"] = followed
    audit = record_action(ctx, action=action, subject_kind="correction", subject_id=correction.id, reason=reason,
                          result=result,
                          input={"original_id": original_id, "dt": dt, "reason": reason,
                                 "narration": narration, "id": entry_id, "mirror_id": mirror_id,
                                 "replacement_id": replacement_id,
                                 "replacement": _replacement_input(resolved)})
    return PostedCorrection(correction=correction, audit=audit)


def reverse_entry(ctx: WriteCtx, *, original_id: str, reason: str, dt: str | None = None,
                  narration: str | None = None, id: str | None = None, mirror_id: str | None = None) -> PostedCorrection:
    """Post an exact mirror of a posted entry beside it, and record why somebody disagreed with it."""
    return _undo(ctx, action="entry.reversed", original_id=original_id, dt=dt or _posted_date(ctx, original_id),
                 reason=reason, replacement=None, narration=narration, entry_id=id, mirror_id=mirror_id,
                 replacement_id=None)


def correct_entry(ctx: WriteCtx, *, original_id: str, reason: str, replacement: Replacement,
                  dt: str | None = None, narration: str | None = None, id: str | None = None,
                  mirror_id: str | None = None, replacement_id: str | None = None) -> PostedCorrection:
    """Reverse a posted entry and post its replacement in one transaction, recording why."""
    return _undo(ctx, action="entry.corrected", original_id=original_id, dt=dt or _posted_date(ctx, original_id),
                 reason=reason, replacement=replacement, narration=narration, entry_id=id, mirror_id=mirror_id,
                 replacement_id=replacement_id)


def _posted_date(ctx: WriteCtx, original_id: str) -> str:
    entry = entries_repo.get_entry(ctx.conn, ctx.entity_id, original_id)
    if entry is None:
        from haddon.domain.errors import not_found
        raise not_found("entry", original_id)
    return entry.dt


@dataclass(frozen=True)
class ClosedPeriod:
    """What a close leaves behind: the lock as it now stands, the record, its snapshot and the checks it ran."""

    lock: closing_repo.Lock
    close_id: str
    snapshot: closing_repo.Snapshot
    checks: CloseChecks
    audit: AuditAction

    def as_json(self) -> dict[str, Any]:
        return {"close_id": self.close_id, "kind": "close",
                "lock": {"through": self.lock.through, "version": self.lock.version, "close_id": self.lock.close_id},
                "snapshot": {"id": self.snapshot.id, "snapshot_seq": self.snapshot.snapshot_seq,
                             "state_hash": self.snapshot.state_hash, "through": self.snapshot.through},
                "checks": self.checks.as_json(), "audit": self.audit.as_json()}


@dataclass(frozen=True)
class ReopenedPeriod:
    """What a reopen leaves behind.  The close it reopened is not touched: it is named, and it stays."""

    lock: closing_repo.Lock
    close_id: str
    reopened_close_id: str | None
    snapshot: closing_repo.Snapshot
    audit: AuditAction

    def as_json(self) -> dict[str, Any]:
        return {"close_id": self.close_id, "kind": "reopen", "reopened_close_id": self.reopened_close_id,
                "lock": {"through": self.lock.through, "version": self.lock.version, "close_id": self.lock.close_id},
                "snapshot": {"id": self.snapshot.id, "snapshot_seq": self.snapshot.snapshot_seq,
                             "state_hash": self.snapshot.state_hash, "through": self.snapshot.through},
                "audit": self.audit.as_json()}


def close_period(ctx: WriteCtx, *, through: str, reason: str, expected_version: int,
                 overrides: Sequence[Override] = ()) -> ClosedPeriod:
    """Shut the books through ``through``, in one transaction, on the acting principal's own authority.

    The whole command is a date, a reason, the lock version the caller believes is current and, only where a
    check is blocking, an override naming that check and saying why.  Everything else the old low-level path
    demanded (a snapshot id, an authorising decision id) is computed here, because a caller who could supply
    them could supply ones that say whatever it liked.

    ``expected_version`` is not ceremony.  Two agents closing the same books from different readings of them is
    exactly the race the scalar lock exists for: the second one fails with ``stale_version`` and its whole
    transaction rolls back, rather than closing over a period it never looked at.  A retry of the same intent
    is a different question and is answered a layer up, by the idempotency key.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise validation("a close needs a reason: it is the sentence a reviewer reads years later",
                         path="reason", reason="required")
    lock, checks, close_id, snapshot = closing_repo.close(ctx, through=through, reason=reason,
                                                          expected_version=expected_version, overrides=overrides)
    audit = record_action(
        ctx, action="period.closed", subject_kind="close_record", subject_id=close_id, reason=reason,
        result={"close_id": close_id, "through": through, "prior_through": checks.prior_through,
                "lock_version": lock.version, "snapshot_id": snapshot.id, "state_hash": snapshot.state_hash,
                "eligible": checks.eligible,
                "checks": {g.key: g.status for g in checks.checks},
                "overrides": [g.waiver.as_json() for g in checks.checks if g.waiver is not None]},
        input={"through": through, "reason": reason, "expected_version": expected_version,
               "overrides": [o.as_json() for o in overrides]})
    return ClosedPeriod(lock=lock, close_id=close_id, snapshot=snapshot, checks=checks, audit=audit)


def reopen_period(ctx: WriteCtx, *, reason: str, expected_version: int, through: str | None = None,
                  close_id: str | None = None) -> ReopenedPeriod:
    """Move the lock deliberately backwards, and say why.

    Reopening is named two ways because people think about it two ways: "open March again" is a boundary, and
    "undo that close" is an action.  ``close_id`` resolves to the boundary that close advanced from, which is the
    only reading of "undo it" that leaves the lock where it stood before.

    It is not gated on the close checks (issue 0005, pitfall 1).  The month that most needs reopening is the
    one that cannot pass them, so gating it would trap the books shut.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise validation("a reopen needs a reason: it is the only record of why a closed period was opened again",
                         path="reason", reason="required")
    if (through is None) == (close_id is None):
        raise validation("name the boundary to reopen to, or the close to undo -- exactly one of them",
                         path="through", reason="target_required")
    reopened: str | None = None
    if close_id is not None:
        record = closing_repo.close_record(ctx.conn, ctx.entity_id, close_id)
        if record is None:
            raise not_found("close_record", close_id)
        if record.kind != "close":
            raise validation(f"close record {close_id!r} is a {record.kind!r}, which reopens nothing",
                             path="close_id", reason="not_a_close", kind=record.kind)
        if record.prior_through is None:
            raise validation(
                f"close {close_id!r} is the first close of this journal, so undoing it would clear the lock "
                f"entirely; reopen to a date instead",
                path="close_id", reason="would_clear_the_lock")
        through, reopened = record.prior_through, close_id
    lock, record_id, snapshot = closing_repo.reopen(ctx, through=through, reason=reason,
                                                    expected_version=expected_version)
    audit = record_action(
        ctx, action="period.reopened", subject_kind="close_record", subject_id=record_id, reason=reason,
        result={"close_id": record_id, "through": through, "reopened_close_id": reopened,
                "lock_version": lock.version, "snapshot_id": snapshot.id, "state_hash": snapshot.state_hash},
        input={"through": through, "close_id": close_id, "reason": reason, "expected_version": expected_version})
    return ReopenedPeriod(lock=lock, close_id=record_id, reopened_close_id=reopened, snapshot=snapshot, audit=audit)
