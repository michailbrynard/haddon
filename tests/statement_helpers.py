"""The statement fixtures the reconciliation tests share: a fictional bank, real CSV bytes, a March statement.

The bank (Calder Union Bank) is fictional and lives only in the tests.  Nothing in ``src/haddon`` knows about it
or about any other bank.

Every fixture statement is built from an actual document (issue 0004, work item 5).  ``render_csv`` writes the
lines out as a small CSV, the bytes are hashed and kept on disk under the test's blob directory, and
``parse_csv`` reads them back into the normalised ``Line``s that are submitted.  So a fixture's lines are what
its document says, its ``sha256`` is the hash of bytes that exist, and ``blob_path`` points at them.  The
provenance the tests assert is real, not a hash of a string.  Pitfall 1 of issue 0004 is a "simpler" model that
quietly stops being able to prove where a number came from.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
import sqlalchemy as sa

import hashlib
from haddon.domain.errors import HaddonError
from haddon.repository import close_checks as cc
from haddon.repository import closing as cl
from haddon.repository import entries as en
from haddon.repository import lines as ln
from haddon.repository import statements as st
from haddon.repository import tables as t
from haddon.reports.balances import trial_balance

from repo_helpers import AGENT, lock_books, post  # noqa: F401  (re-exported)

BANK = "Calder Union Bank"          # invented here; the ledger names no institution
LOCATOR = "page 1, 'Closing balance'"

CASH = "assets:cash"
EUR = "assets:cash-eur"
JPY = "assets:cash-jpy"
OFFICE = "expenses:office"
SALES = "income:sales"

#: Where fixture documents are written.  A directory under the repository's own tree would leak between runs, so
#: the ``blobs`` fixture below sets a fresh one once per test session.
_BLOBS: list[Path] = []

HEADER = ("date", "description", "reference", "amount_micros", "currency")


@pytest.fixture(scope="session", autouse=True)
def blobs(tmp_path_factory):
    """One directory per session for the bytes the fixture documents are actually made of."""
    _BLOBS.clear()
    _BLOBS.append(tmp_path_factory.mktemp("calder-blobs"))
    yield _BLOBS[0]
    _BLOBS.clear()


def blob_dir() -> Path:
    if not _BLOBS:  # a test that builds a document outside a session fixture still needs somewhere to put it
        _BLOBS.append(Path(__import__("tempfile").mkdtemp(prefix="calder-blobs-")))
    return _BLOBS[0]


def render_csv(rows, *, tag: str, currency: str = "USD") -> str:
    """The document itself: the statement as the bank would have handed it over.

    The banner line carries the statement's own name, which is what makes two months with identical figures two
    different documents, as a real download would be.  Without it the bytes of a quiet January and a quiet
    February would be the same, and one document is one statement.
    """
    out = io.StringIO()
    out.write(f"# {BANK} statement {tag}\n")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(HEADER)
    for dt, amount, description, reference in rows:
        writer.writerow([dt, description, reference or "", amount, currency])
    return out.getvalue()


def parse_csv(text: str) -> list[st.Line]:
    """Read the document back into normalised lines, outside the ledger, as an agent would.

    ``raw`` is the line as it stands in the file, so a reviewer comparing the stored line against the original
    document is comparing the same characters.
    """
    body = [ln for ln in text.splitlines() if not ln.startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(body)))
    out = []
    for i, row in enumerate(reader):
        out.append(st.Line(dt=row["date"], description=row["description"],
                           amount_micros=row["amount_micros"], currency=row["currency"],
                           reference=row["reference"] or None, raw=body[i + 1]))
    return out


def write_document(text: str, *, tag: str) -> st.Document:
    """Write the bytes, hash the bytes, and describe them.  The hash is of what is on disk, not of a label."""
    data = text.encode("utf-8")
    path = blob_dir() / f"calder-{tag}.csv"
    path.write_bytes(data)
    return st.Document(sha256=hashlib.sha256(data).hexdigest(), filename=path.name, media_type="text/csv",
                       size_bytes=len(data), blob_path=str(path))


def document_for(rows, *, tag: str, currency: str = "USD") -> tuple[st.Document, list[st.Line]]:
    text = render_csv(rows, tag=tag, currency=currency)
    return write_document(text, tag=tag), parse_csv(text)


def err(ei) -> HaddonError:
    assert isinstance(ei.value, HaddonError)
    return ei.value


def document(tag: str) -> st.Document:
    """A document with no statement behind it, for the tests about registration rather than loading."""
    return write_document(f"# {BANK} {tag}\n", tag=tag)


def row(dt: str, amount: str, *, description: str = f"{BANK} card purchase", reference: str | None = None):
    """One row as it will appear in the CSV: date, amount in micros, description, reference."""
    return (dt, amount, description, reference)


def load(fx, *, account: str = CASH, commodity: str = "USD", start: str = "2026-03-01", end: str = "2026-03-31",
         opening: str = "0", closing: str = "0", rows=(), tag: str = "mar-usd",
         actor: str = AGENT, replaces_id: str | None = None) -> st.Statement:
    doc, lines = document_for(rows, tag=tag, currency=commodity)
    with fx.tx(actor) as ctx:
        return st.load_statement(ctx, account_id=fx.acct(account), commodity=commodity, period_start=start,
                                 period_end=end, opening_micros=opening, closing_micros=closing,
                                 lines=lines, document=doc, locator=LOCATOR, page=1, replaces_id=replaces_id,
                                 reason=f"the {start[:7]} statement, as downloaded")


def quiet_months(fx, *, accounts=(EUR, JPY), start: str = "2026-03-01", end: str = "2026-03-31",
                 suffix: str = "mar") -> None:
    """A zero statement for the other cash accounts, so gate 1 has coverage for every cell of the window."""
    for account, commodity in zip(accounts, ("EUR", "JPY")):
        load(fx, account=account, commodity=commodity, start=start, end=end, opening="0", closing="0",
             rows=(), tag=f"{suffix}-{commodity.lower()}")


def lines_of(fx, statement_id: str) -> list[st.StoredLine]:
    with fx.reader() as conn:
        return st.statement_lines(conn, fx.entity_id, statement_id)


def statement(fx, *, account: str = CASH, closing: str = "-3000000",
              items=(("2026-03-05", "-3000000"),), tag: str = "mar-usd") -> list[st.StoredLine]:
    """One loaded March statement and nothing else.

    The tests about a line (what it books, what it may be linked to, how it is explained) need no
    period lock and no coverage of the other cash accounts, and paying for them in every test would be a slower
    suite for no extra assurance.  ``march`` below is the fuller fixture, for the tests that are about closing a
    month.
    """
    return lines_of(fx, load(fx, account=account, closing=closing, tag=tag,
                             rows=[row(dt, amt) for dt, amt in items]).id)


def march(fx, *, closing: str = "-3000000", items=(("2026-03-05", "-3000000"),)) -> list[st.StoredLine]:
    """The close fixture: February closed (so the window is exactly March), a March statement on the USD cash
    account, and a quiet one on each of the other two so gate 1 has a statement for every cell.

    A first close with no lock starts at the earliest posted entry, so a continuous journal needs one posted
    entry before February can be closed at all; it is non-cash, so it moves nothing gate 1 asks about.
    """
    post(fx, "2025-01-01", "1000000", debit=OFFICE, credit=SALES)
    lock_books(fx, "2026-02-28")
    rows = statement(fx, closing=closing, items=items)
    quiet_months(fx)
    return rows


def book(fx, line_id: str, *, contra: str = OFFICE, amount: str | None = None, commodity: str = "USD",
         narration: str = "office supplies", reason: str = "card purchase against office supplies",
         actor: str = AGENT, **kw) -> ln.Explained:
    """Book one line: post the entry it is, and hold the line against the cash posting it created."""
    with fx.tx(actor) as ctx:
        amount_micros = ctx.conn.execute(sa.select(t.statement_lines.c.amount_micros)
                                         .where(t.statement_lines.c.id == line_id)).scalar_one()
        legs = [en.Leg(fx.acct(contra), commodity, amount if amount is not None else str(-int(amount_micros)))]
        return ln.book_line(ctx, line_id=line_id, legs=legs, narration=narration, reason=reason, **kw)


def refused(fx, call, *, actor: str = AGENT) -> HaddonError:
    """Run a write that must be refused, and prove it left nothing behind.

    ``pytest.raises`` wraps the whole ``write_transaction``, not the call inside it.  A refusal that reached the
    database taints the transaction, and only letting it out of the ``with`` block proves the rollback.
    """
    before = fx.counts()
    with pytest.raises(HaddonError) as ei:
        with fx.tx(actor) as ctx:
            call(ctx)
    assert fx.counts() == before, "a refused write left rows behind"
    return err(ei)


def state(fx, line_id: str) -> ln.LineState:
    with fx.reader() as conn:
        return ln.line_state(conn, fx.entity_id, line_id)


def checks(fx, through: str = "2026-03-31") -> cc.CloseChecks:
    with fx.reader() as conn:
        return cc.close_checks(conn, fx.entity_id, through=through)


def kinds(gate) -> set[str]:
    return {o.kind for o in gate.outstanding}


def close_march(fx, *, actor: str = AGENT, through: str = "2026-03-31", overrides=()) -> cl.Lock:
    """Close with no override at all unless a test asks for one.  This is the acceptance shape."""
    from haddon import commands

    with fx.tx(actor) as ctx:
        lock = cl.get_lock(ctx.conn, ctx.entity_id)
        return commands.close_period(ctx, through=through, reason="the month is complete",
                                     expected_version=lock.version, overrides=list(overrides)).lock


def balances(fx, *, through: str | None = None) -> list[tuple[str, str, str]]:
    """Every account/commodity balance of the journal, as strings.  The comparison a link must not move."""
    with fx.reader() as conn:
        tb = trial_balance(conn, fx.entity_id, through=through, include_zero=True)
    return sorted((line.account_id, line.commodity, line.amount.render()) for line in tb.lines)
