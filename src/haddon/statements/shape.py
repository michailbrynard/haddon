"""Report results arranged as a financial statement: sections, groups, subtotals, and the sign a reader expects.

``haddon.reports`` answers what the books say.  This package answers what a statement of the books looks like,
and the two are deliberately separate: the numbers are computed once, in one place, and everything here is a
rearrangement of them that adds nothing and drops nothing.

Three rules, and they are the whole module.

The sign.  The ledger stores every amount debit-positive, which is why ``income:services`` is negative in a
trial balance.  A statement written that way is correct and unreadable, since nobody writes revenue with a minus
in front of it.  So the credit-natured types (income, liability, equity) are negated for presentation, and every
section then runs positive when it is doing what its name says.  The useful consequence is that an account
sitting the wrong way round, such as a receivable in credit or a payable in debit, shows as a negative inside
its own section instead of being hidden by the flip.

The comparative is a union, not a lookup.  An account with a balance in the prior column and nothing in this one
would vanish from a statement built by walking this period's lines and looking each one up.  It would take its
figure out of the comparative total with it, leaving a total that does not add up and gives no sign of it.  Both
columns are built from every account either of them mentions; absent on one side is a zero there.

Nothing is renamed.  A path's first segment is the section it is already in, so it is dropped; the second
becomes a group heading where more than one account shares it; the rest is the line's label.  No sentence
casing, no abbreviating, no invented display names.  The chart is written by whoever keeps the books, and a
reader looking for ``liabilities:taxes:irs-withholding`` has to be able to find it by reading.  Where stripping
a prefix would leave no label at all (an account posted at its group's own level) the whole path is shown,
which is longer and never ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from haddon.domain.micros import ZERO, Micros
from haddon.reports.types import IncomeStatement, Line, TrialBalance

#: the types the ledger stores credit-negative, and which a statement therefore shows negated
CREDIT_TYPES = ("income", "liability", "equity")


def presented(amount: Micros, account_type: str) -> Micros:
    """Turn one stored amount the way a statement reads it."""
    return -amount if account_type in CREDIT_TYPES else amount


def pretty_path(path: str) -> str:
    """An account path with breathing room.  The separator changes and nothing else does."""
    return " · ".join(path.split(":"))


@dataclass(frozen=True)
class StatementLine:
    account_id: str | None   # None for a line that is not an account (the period's result)
    label: str
    amount: Micros
    comparative: Micros | None


@dataclass(frozen=True)
class StatementGroup:
    heading: str | None      # None when the group holds a single account and needs no heading of its own
    lines: tuple[StatementLine, ...]
    subtotal: Micros | None
    comparative_subtotal: Micros | None


@dataclass(frozen=True)
class StatementSection:
    key: str
    title: str
    total_label: str
    groups: tuple[StatementGroup, ...]
    total: Micros
    comparative_total: Micros | None


@dataclass(frozen=True)
class Extra:
    """A row a section needs that is not an account, such as the period's result inside equity."""

    label: str
    amount: Micros
    comparative: Micros | None


def build_section(key: str, title: str, total_label: str, lines: list[Line], comparative: list[Line],
                  *, show_comparative: bool, extra: tuple[Extra, ...] = ()) -> StatementSection:
    here = {(ln.account_id, ln.commodity): ln for ln in lines}
    then = {(ln.account_id, ln.commodity): ln for ln in comparative}
    union = sorted(set(here) | set(then), key=lambda k: ((here.get(k) or then[k]).path, k[1]))

    by_group: dict[str, list[tuple]] = {}
    for k in union:
        line = here.get(k) or then[k]
        segments = line.path.split(":")
        group = segments[1] if len(segments) > 1 else line.path
        by_group.setdefault(group, []).append(k)

    groups = []
    for group, keys in by_group.items():
        single = len(keys) == 1
        rows = []
        for k in keys:
            line = here.get(k) or then[k]
            stripped = ":".join(line.path.split(":")[1 if single else 2:])
            rows.append(StatementLine(
                account_id=line.account_id,
                label=pretty_path(stripped or line.path),
                amount=presented(here[k].amount if k in here else ZERO, line.type),
                comparative=presented(then[k].amount if k in then else ZERO, line.type) if show_comparative else None,
            ))
        groups.append(StatementGroup(
            heading=None if single else group,
            lines=tuple(rows),
            subtotal=None if single else _sum(r.amount for r in rows),
            comparative_subtotal=None if single or not show_comparative else _sum(r.comparative for r in rows),
        ))

    if extra:
        groups.append(StatementGroup(
            heading=None,
            lines=tuple(StatementLine(None, e.label, e.amount,
                                      (e.comparative or ZERO) if show_comparative else None) for e in extra),
            subtotal=None, comparative_subtotal=None))

    rows = [r for g in groups for r in g.lines]
    return StatementSection(key, title, total_label, tuple(groups), _sum(r.amount for r in rows),
                            _sum(r.comparative for r in rows) if show_comparative else None)


def _sum(values) -> Micros:
    total = ZERO
    for v in values:
        total = total + (v if v is not None else ZERO)
    return total


@dataclass(frozen=True)
class IncomeStatementView:
    """An income statement of one period, with an optional equal period beside it."""

    commodity: str
    start: str
    end: str
    comparative_start: str | None
    comparative_end: str | None
    sections: tuple[StatementSection, ...]
    result: Micros                       # positive is a profit: the service's net, turned the statement's way
    comparative_result: Micros | None

    @property
    def show_comparative(self) -> bool:
        return self.comparative_end is not None


def income_result_label(view: IncomeStatementView) -> str:
    """What the income statement's bottom line is, said rather than left to the sign.

    One place, used by the PDF and the JSON document alike: a zero result is neither a profit nor a loss, and a
    reader told "profit" about a zero would go looking for money that is not there.
    """
    if view.result.is_zero:
        return "Result for the period"
    return "Loss for the period" if view.result.value < 0 else "Profit for the period"


#: the sign convention, said rather than left to be inferred.  Kept for the JSON face, which is read by a
#: caller that has no page to look at; the PDF drops it, because a statement is a form its reader already knows
#: how to read and the sentence was explaining the obvious on every document that leaves this service.
SIGN_NOTE = ("Shown in statement sign: revenue, liabilities and equity read positive when they are what their "
             "name says, so an account standing the other way round appears as a negative inside its own "
             "section.")


def omitted_commodities_note(other_commodities: tuple[str, ...]) -> str:
    """Say what a single-commodity statement leaves out, or return ``""`` when it leaves nothing out.

    A statement is of one commodity, so a document that quietly showed one of several would be short of money
    and say nothing.  This is the one sentence both faces must carry, which is why it is separate from the sign
    convention above: the PDF says only this, the JSON document says both, and neither builds the list itself.
    """
    items = list(other_commodities)
    if not items:
        return ""
    words = items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"
    return (f"These books also hold amounts in {words} in this period, which cannot be added to these and are "
            "not shown.")


def sign_footnote(other_commodities: tuple[str, ...]) -> str:
    """The JSON document's footnote: the sign convention and the omitted-commodity line, in that order.

    Plain text, because a caller renders it wherever it likes.  The PDF draws only ``omitted_commodities_note``,
    the sentence a reader cannot work out from the figures.  So the two faces are not word for word the same
    document, and this is the one place where they differ.
    """
    left_out = omitted_commodities_note(other_commodities)
    return f"{SIGN_NOTE} {left_out}" if left_out else SIGN_NOTE


@dataclass(frozen=True)
class BalanceSheetView:
    """A position at one date, with an optional earlier date beside it.

    ``difference`` is the check, per column: assets less the two sides funding them.  It is zero on a balanced
    journal because equity carries the result.  That is the income and expense movements from the statement's
    period start to the column's date, which nothing has posted to equity yet, and a sheet without it would be out
    by exactly that amount.
    """

    commodity: str
    as_of: str
    comparative_as_of: str | None
    sections: tuple[StatementSection, ...]
    total_assets: Micros
    total_funding: Micros
    difference: Micros
    comparative_total_assets: Micros | None
    comparative_total_funding: Micros | None
    comparative_difference: Micros | None
    checks: tuple[tuple[str, Micros], ...] = field(default_factory=tuple)  # (date, difference) per column shown

    @property
    def show_comparative(self) -> bool:
        return self.comparative_as_of is not None

    @property
    def balances(self) -> bool:
        return all(d.is_zero for _, d in self.checks)


def commodities_of(base: str, *sets: list[Line]) -> list[str]:
    seen = {ln.commodity for s in sets for ln in s}
    if not seen:
        return [base]
    others = sorted(c for c in seen if c != base)
    return [base, *others] if base in seen else sorted(seen)


def _of(lines, commodity: str) -> list[Line]:
    return [ln for ln in lines if ln.commodity == commodity]


def income_statement_view(stmt: IncomeStatement, prior: IncomeStatement | None, *, commodity: str,
                          ) -> IncomeStatementView:
    here, then = _of(stmt.lines, commodity), _of(prior.lines if prior else (), commodity)
    show = prior is not None
    sections = (
        build_section("income", "Revenue", "Total revenue",
                      [l for l in here if l.type == "income"], [l for l in then if l.type == "income"],
                      show_comparative=show),
        build_section("expense", "Expenses", "Total expenses",
                      [l for l in here if l.type == "expense"], [l for l in then if l.type == "expense"],
                      show_comparative=show),
    )
    return IncomeStatementView(
        commodity=commodity, start=stmt.start, end=stmt.end,
        comparative_start=prior.start if prior else None, comparative_end=prior.end if prior else None,
        sections=sections,
        result=-stmt.net.get(commodity, ZERO),
        comparative_result=-prior.net.get(commodity, ZERO) if prior else None,
    )


def balance_sheet_view(sheet: TrialBalance, result: IncomeStatement, prior_sheet: TrialBalance | None,
                       prior_result: IncomeStatement | None, *, commodity: str,
                       result_label: str = "Result to date") -> BalanceSheetView:
    """Build the sheet at a date, with the result that equity carries but nothing has posted to it.

    ``result`` must cover the same span the sheet does, the whole journal through ``sheet.through``, because the
    sheet is cumulative.  The label is deliberately not about a year.  A range is any range now (issue 0002), so a
    row headed "for the year" would be a claim the dates behind it need not support.  Whatever the range, the row
    is every income and expense the journal has accumulated and not appropriated.  That is why it is named for no
    one column: each column runs from the journal's beginning to its own date, and the column heads already say
    which date that is.  ``web/src/routes/Reports.tsx`` labels the same
    row the same way.
    """
    show = prior_sheet is not None and prior_result is not None
    accumulated = presented(result.net.get(commodity, ZERO), "equity")
    prior_accumulated = presented(prior_result.net.get(commodity, ZERO), "equity") if show else None

    def section(kind: str, title: str, total_label: str, extra: tuple[Extra, ...] = ()) -> StatementSection:
        return build_section(kind, title, total_label, _of(sheet.of_type(kind), commodity),
                             _of(prior_sheet.of_type(kind), commodity) if show else [],
                             show_comparative=show, extra=extra)

    assets = section("asset", "Assets", "Total assets")
    liabilities = section("liability", "Liabilities", "Total liabilities")
    equity = section("equity", "Equity", "Total equity", (Extra(result_label, accumulated, prior_accumulated),))

    funding = liabilities.total + equity.total
    prior_funding = (liabilities.comparative_total + equity.comparative_total) if show else None
    difference = assets.total - funding
    prior_difference = (assets.comparative_total - prior_funding) if show else None

    checks = [(sheet.through, difference)]
    if show:
        checks.append((prior_sheet.through, prior_difference))

    return BalanceSheetView(
        commodity=commodity, as_of=sheet.through,
        comparative_as_of=prior_sheet.through if show else None,
        sections=(assets, liabilities, equity),
        total_assets=assets.total, total_funding=funding, difference=difference,
        comparative_total_assets=assets.comparative_total if show else None,
        comparative_total_funding=prior_funding, comparative_difference=prior_difference,
        checks=tuple(checks),
    )
