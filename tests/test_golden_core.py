"""Golden core-invariant fixtures (Phase 0).

The accounting truth is the generic sample journal at ``tests/fixtures/fernside/2025.journal``: a made-up company
in three commodities with one correction.  These tests post the same facts through the typed command layer and
compare what the store computes against the numbers that journal implies.  They assert accounting results at the
command layer (balances, per-commodity equilibrium, correction effect, lock behaviour), not row shapes, so the
schema can change underneath them.

Nothing here needs the sample corpus or the pinned hledger binary: the sample entity is provisioned from scratch
and needs no statement or reconciliation configuration.  The one normalising assumption is that a whole book
balances to zero per commodity, which is the core invariant itself.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pytest

from haddon import commands
from haddon.domain.errors import HaddonError
from haddon.domain.micros import ZERO, Commodity, Micros
from haddon.repository import entries, provisioning
from haddon.repository.entries import Leg
from haddon.reports.balances import income_statement, trial_balance

from repo_helpers import AGENT, lock_books, m

JOURNAL = Path(__file__).parent / "fixtures" / "fernside" / "2025.journal"

TXN = re.compile(r"^(\d{4}-\d{2}-\d{2}) \* (.+)$")
POSTING = re.compile(r"^\s{4}(\S+)\s+(-?[0-9,]+(?:\.[0-9]+)?)\s+(\S+)$")


def _fixture_facts():
    """Read the deliberately small hledger fixture; this is not a product parser.

    The fixture only uses declarations and explicit two-sided postings.  Keeping this reader here makes the
    human-readable journal executable test input without introducing the pinned hledger binary as a unit-test
    dependency or maintaining a second hand-copied set of amounts.
    """
    precisions: dict[str, int] = {}
    transactions: list[tuple[str, str, list[tuple[str, str, str]]]] = []
    current: tuple[str, str, list[tuple[str, str, str]]] | None = None
    for line in JOURNAL.read_text().splitlines():
        if line.startswith("commodity "):
            _, code, display = line.split()
            precisions[code] = len(display.partition(".")[2])
            continue
        if match := TXN.match(line):
            current = (match.group(1), match.group(2), [])
            transactions.append(current)
            continue
        if match := POSTING.match(line):
            assert current is not None, f"posting outside a transaction: {line}"
            account, decimal, code = match.groups()
            amount = Micros.from_decimal(decimal.replace(",", ""),
                                         commodity=Commodity(code, precisions[code])).render()
            current[2].append((account, code, amount))
    assert transactions and all(len(legs) >= 2 for _, _, legs in transactions)
    for dt, narration, legs in transactions:
        totals: dict[str, int] = defaultdict(int)
        for _, commodity, amount in legs:
            totals[commodity] += int(amount)
        assert set(totals.values()) == {0}, f"unbalanced fixture transaction {dt} {narration}: {totals}"
    return precisions, transactions


PRECISIONS, TRANSACTIONS = _fixture_facts()
SAMPLE = TRANSACTIONS[:-1]
REVERSAL_DATE, REVERSAL_NARRATION, _REVERSAL_LEGS = TRANSACTIONS[-1]
REVERSED_NARRATION = "Office supplies"


def _expected_balances() -> dict[str, dict[str, str]]:
    totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for _, _, legs in TRANSACTIONS:
        for account, commodity, amount in legs:
            totals[account][commodity] += int(amount)
    return {account: {commodity: str(amount) for commodity, amount in by_commodity.items() if amount}
            for account, by_commodity in totals.items()}


BALANCES = _expected_balances()
NET = {commodity: str(sum(int(amount) for account, by_commodity in BALANCES.items()
                          if account.startswith(("income:", "expenses:"))
                          for code, amount in by_commodity.items() if code == commodity))
       for commodity in PRECISIONS}


def _bind(fx, account: str, *commodities: str) -> None:
    """The sample entity's chart binds these accounts to the commodities the journal posts to them."""
    with fx.tx(AGENT) as ctx:
        for commodity in commodities:
            provisioning.bind_account_commodity(ctx, account_id=fx.acct(account), commodity=commodity)


def post_sample_journal(fx) -> dict[str, str]:
    """Post every journal transaction and the correction; return a map of narration -> entry id."""
    _bind(fx, "income:sales", "EUR")   # income:sales is USD-only in the shared chart
    _bind(fx, "expenses:office", "JPY")
    ids: dict[str, str] = {}
    with fx.tx(AGENT) as ctx:
        for index, (dt, narration, legs) in enumerate(SAMPLE):
            if index == 0:  # reserved opening provenance is assigned internally, never accepted from a command
                e = entries.post_entry(
                    ctx, dt=dt, narration=narration, origin="opening",
                    legs=[Leg(fx.acct(acct), commodity, m(amount)) for acct, commodity, amount in legs],
                    _allow_reserved_origin=True)
            else:
                e = commands.post_entry(
                    ctx, dt=dt, narration=narration, origin="source", reason="golden journal fixture",
                    legs=[commands.LegSpec(acct, commodity, m(amount)) for acct, commodity, amount in legs]).entry
            ids[narration] = e.id
    with fx.tx(AGENT) as ctx:
        cor = commands.reverse_entry(ctx, original_id=ids[REVERSED_NARRATION], dt=REVERSAL_DATE,
                                     narration=REVERSAL_NARRATION, reason="the supplies were returned")
    ids["mirror"] = cor.correction.mirror_id
    return ids


# A generic entity needs no corpus or statements
def test_the_sample_entity_is_generic_and_needs_no_reconciliation(fx):
    ids = post_sample_journal(fx)
    assert ids["Opening balances"]
    # The chart and commodities came from the sample entity, not a checkout, and no reconciliation is configured.
    assert fx.entity_id == "fernside-instruments"
    for table in ("documents", "statements", "statement_lines", "explanations", "allocations"):
        assert fx.scalar(f"SELECT count(*) FROM {table}") == 0
    assert {c for c in fx.commodities} == {"USD", "EUR", "JPY"}


# Balance, precision and per-commodity equilibrium
def test_trial_balance_matches_the_source_journal(fx):
    post_sample_journal(fx)
    with fx.reader() as conn:
        tb = trial_balance(conn, fx.entity_id, through="2025-12-31")
        for path, by_commodity in BALANCES.items():
            for commodity, amount in by_commodity.items():
                assert tb.get(fx.acct(path), commodity) == m(amount), f"{path} {commodity}"


def test_the_whole_book_balances_to_zero_per_commodity(fx):
    post_sample_journal(fx)
    with fx.reader() as conn:
        tb = trial_balance(conn, fx.entity_id, through="2025-12-31")
    totals: dict[str, Micros] = {}
    for line in tb.lines:
        totals[line.commodity] = totals.get(line.commodity, ZERO) + line.amount
    assert totals == {"USD": ZERO, "EUR": ZERO, "JPY": ZERO}


def test_income_statement_matches_the_source_journal(fx):
    post_sample_journal(fx)
    with fx.reader() as conn:
        pnl = income_statement(conn, fx.entity_id, start="2025-01-01", end="2025-12-31")
    assert pnl.net == {commodity: m(amount) for commodity, amount in NET.items()}


def test_amounts_are_exact_and_precision_is_per_commodity(fx):
    """JPY is 0-decimal: a whole yen is valid and a fraction is refused; a bare Python number is never an amount."""
    _bind(fx, "expenses:office", "JPY")  # otherwise the account/commodity check fires before the quantum check
    assert m("12000000000").value == 12_000_000_000  # 12,000 JPY, exact
    with fx.tx(AGENT) as ctx:
        with pytest.raises(HaddonError) as half_yen:
            entries.post_entry(ctx, dt="2025-05-20", narration="half a yen", origin="source",
                               legs=[Leg(fx.acct("expenses:office"), "JPY", m("500000")),
                                     Leg(fx.acct("assets:cash-jpy"), "JPY", m("-500000"))])
        assert half_yen.value.code == "precision"
    with fx.tx(AGENT) as ctx:
        with pytest.raises(HaddonError) as bare_int:
            entries.post_entry(ctx, dt="2025-05-20", narration="float-ish", origin="source",
                               legs=[Leg(fx.acct("expenses:office"), "USD", 1200),
                                     Leg(fx.acct("assets:cash"), "USD", -1200)])  # type: ignore[arg-type]
        assert bare_int.value.code == "validation"


def test_an_unbalanced_entry_is_refused(fx):
    with fx.tx(AGENT) as ctx:
        from haddon.domain.errors import UNBALANCED

        with pytest.raises(HaddonError) as exc:
            entries.post_entry(ctx, dt="2025-02-10", narration="one-sided", origin="source",
                               legs=[Leg(fx.acct("assets:cash"), "USD", m("1000000000"))])
        assert exc.value.code == UNBALANCED


# Correction: mirror, not edit
def test_a_posted_entry_is_corrected_by_a_mirror_and_never_edited(fx):
    ids = post_sample_journal(fx)
    original_id = ids[REVERSED_NARRATION]
    with fx.reader() as conn:
        original = entries.get_entry(conn, fx.entity_id, original_id)
        mirror = entries.get_entry(conn, fx.entity_id, ids["mirror"])
    assert original is not None and original.status == "posted"       # the original is left standing
    assert mirror is not None and mirror.status == "posted"
    assert (mirror.dt, mirror.origin) == (REVERSAL_DATE, "reversal")

    # The two postings are exact negatives, so the correction changes no balance outside the expense account.
    assert sorted((p.account_id, p.commodity, p.amount.value) for p in mirror.legs) == \
        sorted((p.account_id, p.commodity, -p.amount.value) for p in original.legs)

    with fx.tx(AGENT) as ctx:
        with pytest.raises(HaddonError) as exc:
            entries.revise_draft(ctx, entry_id=original_id, expected_version=original.version, narration="changed")
        assert exc.value.code == "immutable"


def test_a_second_reversal_of_the_same_entry_is_refused(fx):
    ids = post_sample_journal(fx)
    with fx.tx(AGENT) as ctx:
        with pytest.raises(HaddonError) as exc:
            commands.reverse_entry(ctx, original_id=ids[REVERSED_NARRATION], dt="2025-09-01", reason="again")
        assert exc.value.code == "immutable"


# The period lock
def test_a_locked_period_refuses_changes_and_allows_later_ones(fx):
    post_sample_journal(fx)
    lock_books(fx, "2025-03-31")
    with fx.reader() as conn:
        assert entries.lock_through(conn) == "2025-03-31"

    with fx.tx(AGENT) as ctx:
        with pytest.raises(HaddonError) as exc:
            entries.post_entry(ctx, dt="2025-03-05", narration="into the closed month", origin="source",
                               legs=[Leg(fx.acct("expenses:office"), "USD", m("100000000")),
                                     Leg(fx.acct("assets:cash"), "USD", m("-100000000"))])
        assert exc.value.code == "period_locked"

    with fx.tx(AGENT) as ctx:  # the lock is a boundary, not a freeze: later dates still post
        later = entries.post_entry(ctx, dt="2025-04-02", narration="after the lock", origin="source",
                                   legs=[Leg(fx.acct("expenses:office"), "USD", m("100000000")),
                                         Leg(fx.acct("assets:cash"), "USD", m("-100000000"))])
    assert later.status == "posted"


# The fixture file itself
def test_the_sample_journal_exists_and_declares_the_chart_the_tests_use(fx):
    text = JOURNAL.read_text()
    assert text.startswith("; Fernside Instruments")
    for need in ("commodity USD 1,000.00", "commodity EUR 1,000.00", "commodity JPY 1,000.",
                 "account assets:cash", "account income:sales", "account equity:retained-earnings"):
        assert need in text, need
    assert set(fx.commodities) == {"USD", "EUR", "JPY"}
    assert {p for p in BALANCES} == {"assets:cash", "assets:cash-eur", "assets:cash-jpy", "assets:receivables",
                                     "liabilities:payables", "expenses:office", "income:sales",
                                     "equity:retained-earnings"}
