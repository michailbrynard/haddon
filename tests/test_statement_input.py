"""Chunk F1: the generic statement input.

One shape, strictly validated: an account, a period, an opening and a closing balance, a list of lines and the
original document.  There is no reader, no format and no institution anywhere in the ledger.  The bank in these
tests is fictional (Northwind Commercial Bank) and lives only here; nothing in ``src/haddon`` knows about it or
about any other.

What each block below defends:

* The four rules, one at a time.  Each is probed on its own, with a positive control, because a submission
  refused for the wrong reason is a bug that an "it was refused" assertion cannot see.
* A refusal leaves no trace.  There is no half-loaded statement: every refusal is compared against a full
  table-count snapshot taken before it.
* The opening balance.  The declared opening is checked against the prior period's recorded closing, and when
  it cannot be checked that is said out loud.  The first statement matters most: an unverifiable opening must
  survive as the number that was declared and never quietly become zero.
* Provenance, from both ends and when broken.  "Where did this line come from?" has to reach the actual file,
  and if a link disagrees it has to go red rather than return a confident wrong document.
* Gate 1, chunk E2's statement coverage.  Loaded statements give it a genuine pass and a genuine fail; both are
  tested, and so is closing a month with no override at all.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest
import sqlalchemy as sa

from haddon.db.connection import sqlite_connect
from haddon.domain.canonical import sha256_hex
from haddon.domain.errors import HaddonError
from haddon.repository import close_checks as cc
from haddon.repository import closing as cl
from haddon.repository import statements as st
from haddon.repository import tables as t
from haddon.repository.transaction import write_transaction

from haddon import commands
from repo_helpers import AGENT, lock_books, post

#: a bank that does not exist, invented here.  The ledger contains no bank name; a test may.
BANK = "Northwind Commercial Bank"
LOCATOR = "page 2, 'Closing balance'"

CASH = "assets:cash"
EUR = "assets:cash-eur"
JPY = "assets:cash-jpy"


def err(ei) -> HaddonError:
    assert isinstance(ei.value, HaddonError)
    return ei.value


def document(tag: str, **over) -> st.Document:
    """A document reference for a file that would have come from the fictional bank above."""
    fields = {"sha256": sha256_hex(tag), "filename": f"northwind-{tag}.csv", "media_type": "text/csv",
              "size_bytes": 2048, "blob_path": f"/var/haddon/blobs/northwind-{tag}.csv"}
    fields.update(over)
    return st.Document(**fields)


def line(dt: str, amount: str, *, description: str = f"{BANK} card purchase", currency: str = "USD",
         reference: str | None = None, raw: str | None = None) -> st.Line:
    return st.Line(dt=dt, description=description, amount_micros=amount, currency=currency, reference=reference,
                   raw=raw)


def submit(fx, *, account: str = CASH, commodity: str = "USD", start: str = "2026-03-01",
           end: str = "2026-03-31", opening: str = "0", closing: str = "0", lines=(), tag: str = "2026-03",
           locator: str = LOCATOR, page: int | None = 2, reason: str = "the March statement, as downloaded",
           actor: str = AGENT, id: str | None = None, doc: st.Document | None = None,
           replaces: str | None = None) -> st.Statement:
    with fx.tx(actor) as ctx:
        return st.load_statement(ctx, account_id=fx.acct(account), commodity=commodity, period_start=start,
                                   period_end=end, opening_micros=opening, closing_micros=closing,
                                   lines=list(lines), document=doc if doc is not None else document(tag),
                                   locator=locator, page=page, reason=reason, id=id, replaces_id=replaces)


def refused(fx, **kw) -> HaddonError:
    """Submit something that must be refused, and prove the refusal wrote nothing at all."""
    before = fx.counts()
    with pytest.raises(HaddonError) as ei:
        submit(fx, **kw)
    assert fx.counts() == before, "a refused statement left rows behind"
    return err(ei)


# rule 1: the lines must balance
def test_rule_1_the_lines_must_sum_to_closing_minus_opening(fx):
    lines = [line("2026-03-05", "-3000000"), line("2026-03-06", "1000000")]
    e = refused(fx, opening="1000000", closing="0", lines=lines)
    assert (e.code, e.status) == ("unbalanced", 422)
    d = e.details[0]
    assert d["reason"] == "statement_does_not_balance"
    assert (d["lines_micros"], d["expected_micros"], d["difference_micros"]) == ("-2000000", "-1000000", "-1000000")
    assert d["line_count"] == 2
    # positive control: the same lines against the closing they actually imply
    ok = submit(fx, opening="1000000", closing="-1000000", lines=lines, tag="ctl")
    assert (ok.lines_micros, ok.opening_micros, ok.closing_micros) == (-2000000, 1000000, -1000000)


def test_rule_1_catches_a_dropped_line_and_a_duplicated_line(fx):
    """The two ways a parse goes wrong that no per-format check would notice."""
    full = [line("2026-03-05", "-3000000"), line("2026-03-06", "-4000000"), line("2026-03-07", "-5000000")]
    assert refused(fx, opening="0", closing="-12000000", lines=full[:2]).code == "unbalanced"     # dropped
    assert refused(fx, opening="0", closing="-12000000", lines=full + full[2:]).code == "unbalanced"  # duplicated
    assert submit(fx, opening="0", closing="-12000000", lines=full).line_count == 3


def test_rule_1_holds_for_an_empty_statement_too(fx):
    """A month with no transactions is a real statement, and it still has to balance."""
    assert refused(fx, opening="0", closing="-10000", lines=[]).code == "unbalanced"
    quiet = submit(fx, opening="5000000", closing="5000000", lines=[])
    assert quiet.line_count == 0 and quiet.lines_micros == 0
    assert fx.scalar("SELECT count(*) FROM statement_lines WHERE statement_id=?", (quiet.id,)) == 0
    # a quiet month is still a claim about the closing balance, and the record keeps it
    assert fx.scalar("SELECT closing_micros FROM statements WHERE id=?", (quiet.id,)) == 5000000


# rule 2: every line is inside the period
@pytest.mark.parametrize("dt", ["2026-02-28", "2026-04-01"])
def test_rule_2_a_line_dated_outside_the_declared_period_is_refused(fx, dt):
    e = refused(fx, opening="0", closing="-3000000", lines=[line(dt, "-3000000")])
    assert (e.code, e.status) == ("validation", 422)
    d = e.details[0]
    assert d["reason"] == "line_outside_period" and d["dt"] == dt
    assert (d["period_start"], d["period_end"]) == ("2026-03-01", "2026-03-31")
    assert d["path"] == "lines[0].dt"
    # positive control: the boundary days themselves are inside the period
    inside = submit(fx, opening="0", closing="-6000000",
                    lines=[line("2026-03-01", "-3000000"), line("2026-03-31", "-3000000")])
    assert [ln.dt for ln in _lines(fx, inside.id)] == ["2026-03-01", "2026-03-31"]


def test_rule_2_is_also_enforced_by_the_database_underneath(fx):
    """The clean refusal above is pre-validation; the ``statement_lines_insert`` trigger is the guarantee.

    Proven by inserting a line directly through the repository's own write path, bypassing ``load_statement``'s
    check, and confirming the database refuses it anyway.
    """
    loaded = submit(fx, opening="0", closing="0", lines=[])
    with pytest.raises(HaddonError) as ei:
        with fx.tx() as ctx:
            ctx.insert(t.statement_lines, None, "sl", statement_id=loaded.id, line_no=9, source_key="k",
                       content_hash="rh", dt="2026-04-02", amount_micros=-10000, description="d",
                       reference=None, fields="{}", raw="x")
    assert "outside_period" in str(err(ei).details[0]["db_error"])


# rule 3: the currency is declared
def test_rule_3_a_line_with_no_currency_is_refused_and_never_inherits_the_statements(fx):
    for missing in (None, "", "   "):
        e = refused(fx, opening="0", closing="-3000000",
                    lines=[st.Line(dt="2026-03-05", description="d", amount_micros="-3000000", currency=missing)])
        assert (e.code, e.details[0]["reason"]) == ("validation", "required"), missing
        assert e.details[0]["path"] == "lines[0].currency"


def test_rule_3_a_line_in_another_currency_than_the_statement_is_refused(fx):
    e = refused(fx, opening="0", closing="-3000000",
                lines=[line("2026-03-05", "-3000000", currency="EUR")])
    d = e.details[0]
    assert (e.code, d["reason"]) == ("validation", "currency_mismatch")
    assert (d["value"], d["commodity"]) == ("EUR", "USD")


def test_rule_3_the_statements_own_currency_must_be_declared_and_known(fx):
    assert refused(fx, commodity="", opening="0", closing="0").details[0]["reason"] == "required"
    unknown = refused(fx, commodity="XYZ", opening="0", closing="0")
    assert unknown.details[0]["reason"] == "commodity_unknown"
    # known, but not a currency this account holds: the pair close eligibility enumerates
    wrong = refused(fx, account=CASH, commodity="EUR", opening="0", closing="0")
    assert wrong.details[0]["reason"] == "commodity_not_allowed"
    assert submit(fx, account=EUR, commodity="EUR", opening="0", closing="0").commodity == "EUR"


def test_rule_3_the_declared_currency_decides_the_precision(fx):
    """JPY has no decimal places, so a sub-unit amount is a parse error, not something to round."""
    e = refused(fx, account=JPY, commodity="JPY", opening="0", closing="1", tag="jpy",
                lines=[line("2026-03-05", "1", currency="JPY")])
    assert (e.code, e.status) == ("precision", 422)
    assert e.details[0]["reason"] == "quantum" and e.details[0]["commodity"] == "JPY"
    assert submit(fx, account=JPY, commodity="JPY", opening="0", closing="-1000000", tag="jpy2",
                  lines=[line("2026-03-05", "-1000000", currency="JPY")]).closing_micros == -1000000


# rule 4: the same statement loads exactly once
def test_rule_4_the_same_document_cannot_be_loaded_twice(fx):
    first = submit(fx, opening="0", closing="-3000000", lines=[line("2026-03-05", "-3000000")])
    # a different period, a different account, a renamed file -- but the same bytes
    e = refused(fx, account=EUR, commodity="EUR", start="2026-05-01", end="2026-05-31",
                doc=document("2026-03", filename="renamed-copy.csv"))
    assert (e.code, e.status) == ("document_already_loaded", 409)
    d = e.details[0]
    assert d["sha256"] == sha256_hex("2026-03")
    assert d["document_id"] == first.document_id
    assert d["statement_ids"] == [first.id], "the refusal must name the statement it is already loaded as"
    assert d["filename"] == "northwind-2026-03.csv", "the refusal names the file as first loaded, not as re-sent"
    # positive control: different bytes, same everything else
    assert submit(fx, start="2026-05-01", end="2026-05-31", tag="2026-05").document_id != first.document_id


def test_rule_4_is_guaranteed_by_the_database_not_only_by_the_pre_check(fx, monkeypatch):
    """A positive control: with the Python refusal removed, ``statements_insert`` still refuses.

    The pre-check exists to name the statement the document is already loaded as; the *guarantee* is in the
    database, and a second LIVE statement over one document must be impossible either way.  It is a trigger
    rather than the ``UNIQUE(document_id)`` this used to assert, because "one live statement" is derived from
    whether another row replaces you and no key can say that -- see the test below for what the key cost.
    (``documents.sha256`` UNIQUE is the other half: the same bytes are one document however they are named.)
    """
    submit(fx, opening="0", closing="0", tag="dup")
    monkeypatch.setattr(st, "_refuse_reload", lambda *a, **k: None)
    e = refused(fx, start="2026-05-01", end="2026-05-31", tag="dup")
    assert (e.status, e.code) == (409, "document_already_loaded")
    assert e.details[0]["db_error"] == "document_already_loaded"
    assert fx.scalar("SELECT count(*) FROM documents") == 1
    assert fx.scalar("SELECT count(*) FROM statements") == 1


def test_a_parsing_mistake_is_corrected_from_the_original_document(fx):
    """Loaded once, not loaded ever: acceptance criterion 3 for the ordinary single-account case.

    The agent normalises a 6 March transaction as 5 March and loads it.  Nothing is booked.  The correction is
    to re-read the same download properly and submit it as the replacement, with the document's genuine hash.
    A ``UNIQUE(document_id)`` key would refuse that as a reload, so correcting an interpretation would need
    different bytes or a hash that lied about the bytes there were.
    """
    doc = document("2026-03")
    first = submit(fx, opening="0", closing="-3000000", lines=[line("2026-03-05", "-3000000")], doc=doc)
    corrected = submit(fx, opening="0", closing="-3000000", lines=[line("2026-03-06", "-3000000")], doc=doc,
                       reason="the 6 March line was normalised as 5 March", id=None,
                       **{"start": "2026-03-01", "end": "2026-03-31"}, replaces=first.id)
    assert corrected.replaces_id == first.id
    assert corrected.document.sha256 == doc.sha256, "the correction names the original document, unaltered"
    assert fx.scalar("SELECT count(*) FROM documents") == 1, "one document, not a second copy of the same bytes"
    with fx.reader() as conn:
        assert [ln.dt for ln in st.statement_lines(conn, fx.entity_id, corrected.id)] == ["2026-03-06"]
        assert st.get_statement(conn, fx.entity_id, first.id).replaced_by_id == corrected.id


def test_the_document_of_a_replaced_statement_is_free_but_a_live_ones_is_not(fx):
    """The boundary the rule draws: live blocks, replaced does not, and neither is about the bytes changing."""
    doc = document("2026-03")
    first = submit(fx, opening="0", closing="0", doc=doc)
    # while `first` is live, the same document cannot become a second statement beside it
    e = refused(fx, start="2026-05-01", end="2026-05-31", doc=doc)
    assert e.code == "document_already_loaded" and e.details[0]["statement_ids"] == [first.id]
    # once it has been replaced, it is no longer the live statement of that document
    second = submit(fx, opening="0", closing="0", doc=doc, replaces=first.id)
    third = submit(fx, start="2026-05-01", end="2026-05-31", doc=doc, replaces=second.id)
    assert third.replaces_id == second.id


def test_the_recorded_hash_is_the_agents_claim_and_the_ledger_never_checks_it(fx, tmp_path):
    """A known limitation, pinned so it is a decision rather than a surprise (docs/design/decisions.md, "Ingestion
    records what it is told about the bytes, and does not read them").

    Nothing here opens the file.  Load a document with a correct hash, overwrite the bytes at that path, and the
    statement still reports the hash it was given, pointing at a file that no longer hashes to it.  That is the
    ingestion contract: reading the file would put format handling back in the ledger, and could never cover
    ``external_ref`` at all.  What the record claims is "the agent told us these bytes hashed to this", never
    "we checked".  If this test ever has to change, the trigger is in the decision: the first time Haddon serves
    a stored document, the bytes stop being a reference somebody else can check.
    """
    import hashlib

    path = tmp_path / "statement.csv"
    path.write_bytes(b"the statement as it was read\n")
    claimed = hashlib.sha256(path.read_bytes()).hexdigest()
    loaded = submit(fx, opening="0", closing="0",
                    doc=st.Document(sha256=claimed, filename=path.name, media_type="text/csv",
                                    size_bytes=path.stat().st_size, blob_path=str(path)))
    path.write_bytes(b"something else entirely now!!\n")
    with fx.reader() as conn:
        read_back = st.get_statement(conn, fx.entity_id, loaded.id)
    assert read_back.document.sha256 == claimed, "the claim is immutable and permanent"
    assert read_back.document.blob_path == str(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() != claimed, \
        "and it is a claim about bytes the ledger has not looked at"


def test_rule_4_is_content_identity_not_filename_identity(fx):
    submit(fx, opening="0", closing="0", tag="a")
    # the same filename with different bytes is a different statement and is accepted
    other = submit(fx, start="2026-04-01", end="2026-04-30", tag="b",
                   doc=document("b", filename="northwind-2026-03.csv"))
    assert other.document.filename == "northwind-2026-03.csv"


# the period, and what bounds it
def test_an_inverted_period_is_refused(fx):
    assert refused(fx, start="2026-03-31", end="2026-03-01").details[0]["reason"] == "period_inverted"


def test_a_statement_inside_a_locked_period_is_refused(fx):
    post(fx, "2026-02-15", "1000000", debit="expenses:office", credit="income:sales")
    lock_books(fx, "2026-02-28")
    e = refused(fx, start="2026-02-01", end="2026-02-28")
    assert (e.code, e.status) == ("period_locked", 409)
    assert e.details[0]["locked_through"] == "2026-02-28"
    assert submit(fx, start="2026-03-01", end="2026-03-31").period_start == "2026-03-01"


def test_two_statements_may_not_cover_the_same_day_for_one_account_and_currency(fx):
    """The ``period_overlap`` trigger, which compares live statements directly (issue 0004)."""
    first = submit(fx, start="2026-03-01", end="2026-03-31", tag="mar")
    e = refused(fx, start="2026-03-15", end="2026-04-15", tag="overlap")
    assert (e.code, e.status) == ("period_overlap", 409)
    assert e.details[0]["statement_id"] == first.id
    # the same period for a different account and currency is not an overlap
    assert submit(fx, account=EUR, commodity="EUR", start="2026-03-01", end="2026-03-31", tag="eur").id != first.id


def test_the_database_refuses_an_overlap_even_without_the_pre_check(fx, monkeypatch):
    """A positive control on the pre-check: with it removed, the trigger still refuses.

    This is what stops ``_refuse_overlap`` from being the only thing holding the rule up -- the guarantee is the
    trigger, and this test fails if the trigger's own comparison stops holding.
    """
    submit(fx, start="2026-03-01", end="2026-03-31", tag="mar")
    monkeypatch.setattr(st, "_refuse_overlap", lambda *a, **k: None)
    e = refused(fx, start="2026-03-15", end="2026-04-15", tag="overlap")
    assert e.code == "period_overlap" and "period_overlap" in str(e.details[0]["db_error"])


# the opening balance
def test_the_first_statement_records_its_declared_opening_unverified_and_never_as_zero(fx):
    """The case the schema has no column for.  It must not silently become 0."""
    s = submit(fx, opening="7500000", closing="4500000", lines=[line("2026-03-05", "-3000000")])
    assert s.opening_micros == 7500000, "the declared opening was not kept"
    assert s.opening_verified is False
    assert s.opening_basis == st.OPENING_FIRST
    assert s.prior_statement_id is None
    # and it survives a read-back, from the record rather than from the caller's memory
    with fx.reader() as conn:
        back = st.get_statement(conn, fx.entity_id, s.id)
    assert back.opening_micros == 7500000 and back.opening_verified is False
    assert back.opening_basis == st.OPENING_FIRST
    # the number is a column of the immutable statement row (issue 0004), so there is nothing to lose here
    assert fx.scalar("SELECT opening_micros FROM statements WHERE id=?", (s.id,)) == 7500000


def test_the_next_statement_chains_onto_the_prior_recorded_closing(fx):
    feb = submit(fx, start="2026-02-01", end="2026-02-28", opening="0", closing="-3000000", tag="feb",
                 lines=[line("2026-02-05", "-3000000")])
    mar = submit(fx, start="2026-03-01", end="2026-03-31", opening="-3000000", closing="-8000000", tag="mar",
                 lines=[line("2026-03-05", "-5000000")])
    assert mar.opening_verified is True
    assert mar.opening_basis == st.OPENING_PRIOR
    assert mar.prior_statement_id == feb.id


def test_an_opening_that_disagrees_with_the_prior_closing_is_refused(fx):
    feb = submit(fx, start="2026-02-01", end="2026-02-28", opening="0", closing="-3000000", tag="feb",
                 lines=[line("2026-02-05", "-3000000")])
    e = refused(fx, start="2026-03-01", end="2026-03-31", opening="-2990000", closing="-7990000", tag="mar",
                lines=[line("2026-03-05", "-5000000")])
    d = e.details[0]
    assert (e.code, d["reason"]) == ("validation", "opening_does_not_match_prior_statement")
    assert (d["declared_micros"], d["prior_micros"]) == ("-2990000", "-3000000")
    assert (d["prior_statement_id"], d["prior_end_dt"]) == (feb.id, "2026-02-28")


def test_a_gap_before_the_period_leaves_the_opening_unverified_and_names_the_gap(fx):
    submit(fx, start="2026-01-01", end="2026-01-31", opening="0", closing="-3000000", tag="jan",
           lines=[line("2026-01-05", "-3000000")])
    mar = submit(fx, start="2026-03-01", end="2026-03-31", opening="-3000000", closing="-3000000", tag="mar")
    assert mar.opening_verified is False
    assert mar.opening_basis == st.OPENING_GAP
    assert mar.prior_statement_id is None


def test_an_opening_is_chained_per_account_and_currency_not_globally(fx):
    submit(fx, start="2026-02-01", end="2026-02-28", opening="0", closing="-3000000", tag="feb-usd",
           lines=[line("2026-02-05", "-3000000")])
    # the EUR account has no history at all, so its March opening is a first statement, not a chain onto USD
    eur = submit(fx, account=EUR, commodity="EUR", start="2026-03-01", end="2026-03-31", opening="900000",
                 closing="900000", tag="mar-eur")
    assert eur.opening_basis == st.OPENING_FIRST and eur.opening_micros == 900000


def test_the_opening_cannot_be_read_back_as_zero_because_it_is_a_column(fx):
    """The declared opening survives the read, and nothing can edit it away afterwards.

    It is ``statements.opening_micros`` (issue 0004): NOT NULL, immutable, and read straight off the row, so
    there is no path on which it can be absent and silently read as 0.
    """
    s = submit(fx, opening="7500000", closing="7500000")
    with fx.reader() as conn:
        assert st.get_statement(conn, fx.entity_id, s.id).opening_micros == 7500000
    assert fx.scalar("SELECT opening_micros FROM statements WHERE id=?", (s.id,)) == 7500000
    with pytest.raises(HaddonError) as ei:
        with fx.tx() as ctx:
            ctx.execute(t.statements.update().where(t.statements.c.id == s.id).values(opening_micros=0))
    assert err(ei).code == "immutable"

# the document reference
def test_the_document_reference_is_validated_strictly(fx):
    bad = [({"sha256": "abc"}, "sha256_shape"), ({"sha256": "A" * 64}, "sha256_shape"),
           ({"filename": ""}, "required"), ({"media_type": " "}, "required"),
           ({"blob_path": ""}, "one_location_required"),
           ({"blob_path": "/z", "external_ref": "dms://9"}, "one_location_required"),
           ({"size_bytes": -1}, "size_invalid"), ({"size_bytes": "2048"}, "size_invalid")]
    for over, reason in bad:
        assert refused(fx, doc=document("d", **over)).details[0]["reason"] == reason, over
    assert refused(fx, locator="").details[0]["reason"] == "required"
    assert refused(fx, page=0).details[0]["reason"] == "page_invalid"
    assert refused(fx, reason=" ").details[0]["path"] == "reason"


def test_the_document_is_stored_against_the_statement_and_reaches_every_line(fx):
    s = submit(fx, opening="0", closing="-3000000", lines=[line("2026-03-05", "-3000000")])
    row = fx.q("SELECT sha256, filename, media_type, size_bytes, blob_path FROM documents WHERE id=?",
               (s.document_id,))[0]
    assert row == (sha256_hex("2026-03"), "northwind-2026-03.csv", "text/csv", 2048,
                   "/var/haddon/blobs/northwind-2026-03.csv")
    # A line does not repeat its document: it belongs to the statement, and the statement names the document
    # once.  The chain is one join, the one `read_provenance` walks.
    assert fx.q("SELECT DISTINCT s.document_id FROM statement_lines l JOIN statements s ON s.id=l.statement_id "
                "WHERE l.statement_id=?", (s.id,)) == [(s.document_id,)]
    assert "document_id" not in {r[0] for r in fx.q("SELECT name FROM pragma_table_info('statement_lines')")}


def test_the_load_writes_one_statement_one_document_and_one_evidence_link(fx):
    """What a load costs, counted.  Issue 0004's claim is that a load is five rows and not nine kinds of row,
    so the count is asserted rather than described."""
    s = submit(fx, opening="0", closing="-3000000", lines=[line("2026-03-05", "-3000000", reference="TX-8891")],
               reason="loaded from the bank portal download")
    assert fx.scalar("SELECT count(*) FROM statements") == 1
    assert fx.scalar("SELECT count(*) FROM documents") == 1
    assert fx.scalar("SELECT count(*) FROM statement_lines") == 1
    assert fx.scalar("SELECT count(*) FROM evidence_links") == 1
    assert fx.scalar("SELECT reason FROM statements WHERE id=?", (s.id,)) == "loaded from the bank portal download"
    # no approval chain (issue 0005) and no synthetic source configuration (issue 0004): the tables do not exist
    names = {r[0] for r in fx.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not names & {"proposals", "decisions", "applications", "source_configs"}
    # the evidence link points the auditor at the page the closing balance is printed on
    link = fx.q("SELECT entry_id, statement_id, document_id, locator FROM evidence_links")[0]
    assert (link[0], link[1], link[2], link[3]) == (None, s.id, s.document_id, LOCATOR)


def test_an_implausibly_large_submission_and_over_long_text_are_refused(fx):
    """Bounds, so one submission cannot be a denial of service or an unbounded row.

    ``MAX_LINES`` is refused *before* anything is written, which is why the count check comes first in
    ``check_lines`` rather than after the per-line work.
    """
    too_many = [line("2026-03-05", "0") for _ in range(st.MAX_LINES + 1)]
    e = refused(fx, opening="0", closing="0", lines=too_many)
    assert e.details[0]["reason"] == "too_many"
    assert (e.details[0]["count"], e.details[0]["maximum"]) == (st.MAX_LINES + 1, st.MAX_LINES)
    over = refused(fx, opening="0", closing="0",
                   lines=[line("2026-03-05", "0", description="x" * 4097)])
    assert over.details[0]["reason"] == "too_long" and over.details[0]["path"] == "lines[0].description"
    raw = refused(fx, opening="0", closing="0",
                  lines=[line("2026-03-05", "0", raw="x" * 8193)])
    assert raw.details[0]["reason"] == "too_long" and raw.details[0]["path"] == "lines[0].raw"


def test_an_amount_outside_the_signed_64_bit_range_is_refused_not_wrapped(fx):
    assert refused(fx, opening="0", closing="9223372036854775808").code == "overflow"
    assert refused(fx, opening="0", closing="0",
                   lines=[line("2026-03-05", "9223372036854775808")]).code == "overflow"


# "where did this line come from?"
def _lines(fx, statement_id: str) -> list[st.StoredLine]:
    with fx.reader() as conn:
        return st.statement_lines(conn, fx.entity_id, statement_id)


def corrupt(fx, drop: str, sql: str, params: tuple = (), *, foreign_keys: bool = True) -> None:
    """Create a state the production triggers forbid, on the test's own copy of the database.

    Used only to prove that a *read* refuses a broken chain.  It drops one immutability trigger and then writes;
    nothing in ``src/haddon`` can reach this path, and no production trigger is changed.
    """
    con = sqlite_connect(fx.path)
    try:
        if not foreign_keys:
            con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")
        con.execute(drop)
        con.execute(sql, params)
        con.execute("COMMIT")
    finally:
        con.close()


def test_where_did_this_line_come_from_reaches_the_actual_document(fx):
    s = submit(fx, opening="0", closing="-3000000",
               lines=[line("2026-03-05", "-3000000", description="NORTHWIND CARD 4821 COFFEE",
                           reference="TX-8891", raw="2026-03-05,NORTHWIND CARD 4821 COFFEE,-30.00,TX-8891")])
    row = _lines(fx, s.id)[0]
    with fx.reader() as conn:
        prov = st.read_provenance(conn, fx.entity_id, line_id=row.id)
    assert prov.subject_kind == "line" and len(prov.lines) == 1
    ln = prov.lines[0]
    assert ln.document.sha256 == sha256_hex("2026-03")
    assert ln.document.filename == "northwind-2026-03.csv"
    assert ln.document.blob_path == "/var/haddon/blobs/northwind-2026-03.csv"
    assert ln.statement_id == s.id and (ln.period_start, ln.period_end) == ("2026-03-01", "2026-03-31")
    assert ln.line.line_no == 1 and ln.line.reference == "TX-8891"
    assert ln.line.raw == "2026-03-05,NORTHWIND CARD 4821 COFFEE,-30.00,TX-8891"
    assert ln.allocations == (), "nothing explains this line yet, and that is said rather than guessed"
    assert ln.statement_live is True
    # The document reported must be the one the line's own statement points at, read independently out of SQL
    # rather than compared against what this test submitted.
    expected = fx.q("SELECT d.sha256, d.filename, d.blob_path FROM statement_lines l "
                    "JOIN statements st ON st.id = l.statement_id JOIN documents d ON d.id = st.document_id "
                    "WHERE l.id=?", (row.id,))[0]
    assert (ln.document.sha256, ln.document.filename, ln.document.blob_path) == expected


def test_a_line_with_no_raw_text_keeps_the_normalised_line_verbatim(fx):
    s = submit(fx, opening="0", closing="-3000000", lines=[line("2026-03-05", "-3000000", description="fee")])
    stored = json.loads(_lines(fx, s.id)[0].raw)
    assert stored == {"dt": "2026-03-05", "amount_micros": "-3000000", "description": "fee",
                      "reference": None, "currency": "USD"}


def test_provenance_reaches_the_document_from_a_posting_and_from_an_entry(seeded_engine):
    """The other direction: a posting, through the allocation that explains it, back to the file.

    The seeded fixture is the one in the build with live allocations already in place, which is exactly what
    this direction traverses.
    """
    with seeded_engine.connect() as conn:
        by_posting = st.read_provenance(conn, "E", posting_id="e_posted.p1")
        by_entry = st.read_provenance(conn, "E", entry_id="e_posted")
        unexplained = st.read_provenance(conn, "E", posting_id="e_draft.p1")
    assert [ln.line.id for ln in by_posting.lines] == ["r1"]
    assert by_posting.lines[0].document.filename == "b.pdf"
    assert by_posting.lines[0].document.sha256 == "s2"
    assert by_posting.lines[0].allocations[0]["posting_id"] == "e_posted.p1"
    assert by_posting.lines[0].allocations[0]["kind"] == "linked"
    # the entry reaches the same line, plus the evidence linked to the entry itself
    assert [ln.line.id for ln in by_entry.lines] == ["r1"]
    assert [e["evidence_id"] for e in by_entry.evidence] == ["ev1"]
    assert by_entry.evidence[0]["document"]["filename"] == "b.pdf"
    # a posting nothing explains answers "nothing links this yet" rather than inventing a document
    assert unexplained.lines == () and unexplained.evidence == ()


def test_a_superseded_explanation_no_longer_claims_the_posting_it_used_to(seeded_engine):
    """r_eur was ignored, then linked instead.  Provenance follows the live explanation only.  A superseded one
    is history, and history must not answer "what explains this posting?"."""
    with seeded_engine.connect() as conn:
        prov = st.read_provenance(conn, "E", posting_id="e_eur.p1")
    assert [ln.line.id for ln in prov.lines] == ["r_eur"]
    assert [a["explanation_id"] for a in prov.lines[0].allocations] == ["x_new"]


def test_a_broken_provenance_link_refuses_instead_of_naming_the_wrong_document(fx):
    """The chain a line hangs from is line -> statement -> document, and every link is a foreign key.

    A line can no longer point at a document *directly*, which is how it used to be able to point at the wrong
    one; its document is whatever its statement says.  What is still possible on a restored or hand-edited file
    is a statement pointing at a document the entity does not hold, and that refuses rather than degrading.
    """
    s = submit(fx, opening="0", closing="-3000000", lines=[line("2026-03-05", "-3000000")])
    row = _lines(fx, s.id)[0]
    corrupt(fx, "DROP TRIGGER statements_u", "UPDATE statements SET document_id='doc-ghost' WHERE id=?",
            (s.id,), foreign_keys=False)
    with fx.reader() as conn:
        with pytest.raises(HaddonError) as ei:
            st.read_provenance(conn, fx.entity_id, line_id=row.id)
    e = err(ei)
    assert e.code == "internal_error" and "doc-ghost" in e.message


def test_provenance_needs_exactly_one_subject_and_404s_on_one_we_do_not_hold(fx):
    with fx.reader() as conn:
        for kwargs in ({}, {"line_id": "a", "posting_id": "b"}):
            with pytest.raises(HaddonError) as ei:
                st.read_provenance(conn, fx.entity_id, **kwargs)
            assert err(ei).details[0]["reason"] == "one_subject_required"
        for kind, kwargs in (("statement line", {"line_id": "nope"}), ("posting", {"posting_id": "nope"}),
                             ("entry", {"entry_id": "nope"})):
            with pytest.raises(HaddonError) as ei:
                st.read_provenance(conn, fx.entity_id, **kwargs)
            assert (err(ei).code, err(ei).status) == ("not_found", 404)
            assert err(ei).details[0]["kind"] == kind


# chunk E2's gate 1 becomes a real answer
def _cover_march(fx, *, closing: str = "0", lines=()) -> None:
    """One March statement for every cash account/currency pair of the chart, so gate 1 has full coverage."""
    for tag, account, commodity in (("usd", CASH, "USD"), ("eur", EUR, "EUR"), ("jpy", JPY, "JPY")):
        submit(fx, account=account, commodity=commodity, start="2026-03-01", end="2026-03-31",
               opening="0", closing=closing if account == CASH else "0",
               lines=lines if account == CASH else (), tag=f"mar-{tag}")


def _seed_window(fx) -> None:
    """One posted entry before the close window: a continuous journal has no book to establish one, and a first
    close starts at the earliest posted entry.  It is deliberately on non-cash accounts so it moves no statement's
    account."""
    post(fx, "2026-02-15", "1000000", debit="expenses:office", credit="income:sales")


def test_loading_statements_turns_gate_1_from_incomplete_into_a_real_pass(fx):
    _seed_window(fx)
    lock_books(fx, "2026-02-28")
    with fx.reader() as conn:
        before = cc.close_checks(conn, fx.entity_id, through="2026-03-31").check("statement_coverage")
    assert before.status == cc.FAIL
    assert {o.kind for o in before.outstanding} == {"statement_not_loaded"}

    _cover_march(fx)

    with fx.reader() as conn:
        checks = cc.close_checks(conn, fx.entity_id, through="2026-03-31")
    gate = checks.check("statement_coverage")
    assert gate.status == cc.PASS, gate.outstanding
    assert gate.outstanding == ()
    # and the two substrate-gated checks stop being unevaluable, because a statement now covers every cell
    for key in ("lines_explained", "nothing_partial"):
        assert checks.check(key).status == cc.PASS, checks.check(key).reason
    assert checks.eligible is True and checks.may_close is True


def test_a_statement_whose_closing_disagrees_with_the_ledger_is_a_real_fail_not_unevaluable(fx):
    _seed_window(fx)
    lock_books(fx, "2026-02-28")
    # the statement says the account moved; the ledger has nothing posted in March
    _cover_march(fx, closing="-3000000", lines=[line("2026-03-05", "-3000000")])
    with fx.reader() as conn:
        gate = cc.close_checks(conn, fx.entity_id, through="2026-03-31").check("statement_coverage")
    assert gate.status == cc.FAIL, "a loaded statement that disagrees must fail, not be unevaluable"
    disagreements = [o for o in gate.outstanding if o.kind == "closing_balance_disagrees"]
    assert len(disagreements) == 1
    assert disagreements[0].detail["asserted_micros"] == "-3000000"
    assert disagreements[0].detail["ledger_micros"] == "0"
    assert disagreements[0].detail["difference_micros"] == "3000000"


def test_a_month_covered_by_statements_closes_with_no_override_at_all(fx):
    """The end-to-end point of the chunk: completeness, not a waiver, is what lets a month close."""
    _seed_window(fx)
    lock_books(fx, "2026-02-28")
    _cover_march(fx)
    with fx.tx() as ctx:
        lock = cl.get_lock(ctx.conn, ctx.entity_id)
        closed = commands.close_period(ctx, through="2026-03-31",
                                       reason="March closes because March is covered",
                                       expected_version=lock.version, overrides=[])
    assert closed.lock.through == "2026-03-31"
    assert closed.checks.eligible is True
    assert fx.q("SELECT overrides FROM close_records WHERE id=?", (closed.close_id,)) == [("[]",)]


def test_a_statement_that_agrees_with_a_posted_ledger_passes_gate_1(fx):
    """The realistic shape: the month has bookkeeping in it and the statement agrees with the books."""
    _seed_window(fx)
    lock_books(fx, "2026-02-28")
    post(fx, "2026-03-05", "3000000")  # +3.00 on expenses:office, -3.00 on assets:cash
    for tag, account, commodity in (("usd", CASH, "USD"), ("eur", EUR, "EUR"), ("jpy", JPY, "JPY")):
        submit(fx, account=account, commodity=commodity, start="2026-03-01", end="2026-03-31", opening="0",
               closing="-3000000" if account == CASH else "0",
               lines=[line("2026-03-05", "-3000000")] if account == CASH else (), tag=f"mar-{tag}")
    with fx.reader() as conn:
        gate = cc.close_checks(conn, fx.entity_id, through="2026-03-31").check("statement_coverage")
    assert gate.status == cc.PASS, gate.outstanding


# the architecture, pinned
def test_the_statement_input_parses_nothing_and_reads_no_file():
    """The decision this chunk implements, as a structural guard rather than a comment.

    Haddon holds no bank-specific and no format-specific code.  The way that could erode is a convenience
    "just read the CSV here" helper appearing in the statement path, so the check is on what the modules import
    and call, not on their prose: neither statement module may reach a parsing library or open a file.
    """
    import ast
    import pathlib

    import haddon

    root = pathlib.Path(haddon.__file__).parent
    forbidden_modules = {"csv", "codecs", "openpyxl", "xlrd", "pandas", "pdfplumber", "PyPDF2", "pypdf",
                         "chardet", "io", "pathlib", "os", "shutil", "tempfile", "subprocess"}
    forbidden_calls = {"open", "exec", "eval", "compile"}
    offenders = []
    for rel in ("repository/statements.py", "api/routes/statements.py"):
        tree = ast.parse((root / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_modules:
                        offenders.append(f"{rel}: imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in forbidden_modules:
                offenders.append(f"{rel}: imports from {node.module}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                offenders.append(f"{rel}:{node.lineno}: calls {node.func.id}()")
            elif isinstance(node, ast.Attribute) and node.attr in {"read_text", "read_bytes", "open"}:
                offenders.append(f"{rel}:{node.lineno}: reaches for {node.attr}")
    assert offenders == [], "the statement input started reading files:\n" + "\n".join(offenders)


def test_the_service_depends_on_no_parsing_library():
    """The same decision at the dependency level: nothing in the install set can read a bank format."""
    import pathlib
    import tomllib

    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    declared = " ".join(project["dependencies"]).lower()
    for library in ("openpyxl", "xlrd", "pandas", "pdfplumber", "pypdf", "tabula", "camelot", "chardet"):
        assert library not in declared, f"{library} is a dependency; the ledger must not parse bank formats"


def test_the_statement_schema_and_api_name_no_institution():
    """No bank name and no company name on the input surface.  The fictional one in this file stays here."""
    import pathlib

    import haddon

    root = pathlib.Path(haddon.__file__).parent
    text = "\n".join((root / rel).read_text().lower()
                     for rel in ("repository/statements.py", "api/routes/statements.py"))
    sql = "\n".join(p.read_text().lower() for p in sorted((root / "db/migrations/sql").rglob("*.sql")))
    import re
    for name in ("northwind", "chase", "barclays", "hsbc", "monzo"):
        pattern = re.compile(rf"\b{name}\b")  # word-bounded: "chased" is not the bank called Chase
        assert not pattern.search(text), f"{name!r} appears in the statement input"
        assert not pattern.search(sql), f"{name!r} appears in the migrated schema"


# the HTTP surface
def wire_document(tag: str, **over) -> dict:
    body = {"sha256": sha256_hex(tag), "filename": f"northwind-{tag}.csv", "media_type": "text/csv",
            "size_bytes": 2048, "blob_path": f"/var/haddon/blobs/northwind-{tag}.csv"}
    body.update(over)
    return body


def wire_statement(account: str, **over) -> dict:
    """The submission as an agent writes it (issue 0006): an account path and decimal amounts.

    The statement the ledger stores is in micro-units, checked to the commodity's quantum, and every amount in
    the reply below is an integer micro string.  Only the input vocabulary is decimal.
    """
    body = {"account": account, "commodity": "USD", "period_start": "2026-03-01",
            "period_end": "2026-03-31", "opening": "0", "closing": "-3.00",
            "lines": [{"dt": "2026-03-05", "description": "NORTHWIND CARD 4821", "amount": "-3.00",
                       "currency": "USD", "reference": "TX-8891",
                       "raw": "2026-03-05,NORTHWIND CARD 4821,-30.00,TX-8891"}],
            "document": wire_document("2026-03"), "locator": LOCATOR, "page": 2,
            "reason": "the March statement, as downloaded"}
    body.update(over)
    return body


def cash_id(api) -> str:
    return next(a["id"] for a in api.json("/accounts")["items"] if a["path"] == CASH)


def seed_window_http(api) -> None:
    """Give this entity a first posted entry.  A continuous journal's first close starts at the earliest posted
    entry, and an entity that has never posted cannot be evaluated at all.  Non-cash on purpose, so it moves no
    statement's account."""
    r = api.post("/entries", key=uuid.uuid4().hex, json={
        "dt": "2026-02-15", "narration": "opening accrual", "reason": "give the journal a first posted entry",
        "legs": [{"account": "expenses:office", "commodity": "USD", "amount": "1.00"},
                 {"account": "income:sales", "commodity": "USD", "amount": "-1.00"}]})
    assert r.status_code == 201, r.text


def test_the_endpoint_loads_a_statement_and_reads_it_back_with_its_document(api_small):
    aid = cash_id(api_small)
    created = api_small.post("/statements", key=uuid.uuid4().hex, json=wire_statement(aid))
    assert created.status_code == 201, created.text
    s = created.json()["statement"]
    assert s["account_path"] == CASH
    assert s["opening_micros"] == "0" and s["closing_micros"] == "-3000000"
    assert s["lines_micros"] == "-3000000" and s["line_count"] == 1
    assert s["opening_verified"] is False and s["opening_basis"] == st.OPENING_FIRST
    # Issue 0007: the document goes out without ``blob_path``.  A path on the service's own filesystem is not
    # something a reader can open or a response should publish.  ``storage`` is published instead, and the
    # bytes are at ``/documents/{id}/content``.
    submitted = wire_document("2026-03")
    assert s["document"] == {**{k: v for k, v in submitted.items() if k != "blob_path"},
                             "storage": "stored", "external_ref": None}
    assert api_small.json(f"/statements/{s['statement_id']}")["statement"] == s
    lines = api_small.json(s["links"]["lines"])
    assert [ln["reference"] for ln in lines["items"]] == ["TX-8891"]
    # and the provenance link on the line reaches the file
    prov = api_small.json(lines["items"][0]["links"]["provenance"])
    assert prov["subject"] == {"kind": "line", "id": lines["items"][0]["line_id"]}
    assert prov["lines"][0]["document"]["sha256"] == sha256_hex("2026-03")
    # the id, not only the content: a reader that can see a document's name and hash but cannot address it has
    # no way to ask for the bytes, which is the whole of what a provenance trail is for (issue 0007)
    assert prov["lines"][0]["document_id"] == s["document_id"]
    assert prov["lines"][0]["raw"] == "2026-03-05,NORTHWIND CARD 4821,-30.00,TX-8891"


def test_the_endpoint_refuses_each_rule_with_its_own_status_and_code(api_small):
    aid = cash_id(api_small)
    cases = [
        (wire_statement(aid, closing="0"), 422, "unbalanced"),
        (wire_statement(aid, period_start="2026-04-01", period_end="2026-04-30"), 422, "validation"),
        (wire_statement(aid, commodity="EUR"), 422, "validation"),
    ]
    for body, status, code in cases:
        r = api_small.post("/statements", key=uuid.uuid4().hex, json=body)
        assert (r.status_code, r.json()["error"]["code"]) == (status, code), body
    # rule 4 needs a successful load first
    assert api_small.post("/statements", key=uuid.uuid4().hex, json=wire_statement(aid)).status_code == 201
    again = api_small.post("/statements", key=uuid.uuid4().hex,
                           json=wire_statement(aid, period_start="2026-05-01", period_end="2026-05-31",
                                               lines=[], closing="0"))
    assert (again.status_code, again.json()["error"]["code"]) == (409, "document_already_loaded")
    assert api_small.scalar("SELECT count(*) FROM statements") == 1


def test_the_endpoint_refuses_a_json_number_for_an_amount_in_either_direction(api_small):
    aid = cash_id(api_small)
    for body in (wire_statement(aid, closing=-3),
                 wire_statement(aid, lines=[{"dt": "2026-03-05", "description": "d", "amount": -3,
                                             "currency": "USD"}]),
                 wire_statement(aid, opening=0.0)):
        r = api_small.post("/statements", key=uuid.uuid4().hex, json=body)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["details"][0]["reason"] in {"string_required", "float_rejected"}
    # outbound: not one amount in any statement response is a JSON number
    assert api_small.post("/statements", key=uuid.uuid4().hex, json=wire_statement(aid)).status_code == 201
    for path in ("/statements", f"/statements/{api_small.scalar('SELECT id FROM statements')}"):
        for key, value in _walk(api_small.json(path)):
            if key.endswith("_micros"):
                assert isinstance(value, str), (path, key, value)


def _walk(node, prefix: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, k)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, prefix)
    else:
        yield prefix, node


def test_the_endpoint_requires_an_idempotency_key_and_replays_the_same_intent(api_small):
    aid = cash_id(api_small)
    body = wire_statement(aid)
    assert api_small.post("/statements", json=body).status_code == 422
    key = uuid.uuid4().hex
    first = api_small.post("/statements", key=key, json=body)
    assert first.status_code == 201
    replay = api_small.post("/statements", key=key, json=body)
    assert (replay.status_code, replay.headers.get("Idempotency-Replayed")) == (201, "true")
    assert replay.json() == first.json()
    assert api_small.scalar("SELECT count(*) FROM statements") == 1
    # the same key with a different intent is a conflict, not a second load and not a replay
    other = api_small.post("/statements", key=key, json=wire_statement(aid, reason="something else"))
    assert (other.status_code, other.json()["error"]["code"]) == (409, "idempotency_conflict")


def test_the_endpoint_lists_and_filters_statements(api_small):
    aid = cash_id(api_small)
    eur = next(a["id"] for a in api_small.json("/accounts")["items"] if a["path"] == EUR)
    api_small.post("/statements", key=uuid.uuid4().hex, json=wire_statement(aid))
    api_small.post("/statements", key=uuid.uuid4().hex,
                   json=wire_statement(eur, commodity="EUR", document=wire_document("eur"),
                                       lines=[{"dt": "2026-03-05", "description": "d",
                                               "amount": "-3.00", "currency": "EUR"}]))
    assert len(api_small.json("/statements")["items"]) == 2
    assert [i["commodity"] for i in api_small.json("/statements?commodity=EUR")["items"]] == ["EUR"]
    assert [i["account_id"] for i in api_small.json(f"/statements?account_id={aid}")["items"]] == [aid]
    assert api_small.json(f"/statements?account_id=acct-nope")["items"] == []


def test_an_unknown_statement_and_an_unknown_provenance_subject_are_404(api_small):
    assert api_small.get("/statements/nope").status_code == 404
    assert api_small.get("/statements/nope/lines").status_code == 404
    assert api_small.get("/provenance?line_id=nope").status_code == 404
    assert api_small.get("/provenance").status_code == 422


#: measured at 0.5 s or more (chunk E3): generating the whole OpenAPI document is the cost, not the route.
@pytest.mark.slow
def test_the_statement_endpoints_are_published_and_scoped_to_this_entity(api_small):
    doc = api_small.client.get("/openapi.json").json()
    paths = set(doc["paths"])
    for path in ("/v1/entities/{entity_id}/statements", "/v1/entities/{entity_id}/statements/{statement_id}",
                 "/v1/entities/{entity_id}/statements/{statement_id}/lines",
                 "/v1/entities/{entity_id}/provenance"):
        assert path in paths, path
    caps = api_small.json("/capabilities")
    assert f"POST {api_small.base}/statements" in caps["writes"]
    assert f"{api_small.base}/provenance" in caps["reads"]
    # a credential that is not scoped here cannot load a statement into this entity
    from conftest import OTHER_TOKEN
    blocked = api_small.post("/statements", key=uuid.uuid4().hex, token=OTHER_TOKEN,
                             json=wire_statement(cash_id(api_small)))
    assert blocked.status_code in (403, 404)
    assert api_small.scalar("SELECT count(*) FROM statements") == 0


def test_loading_a_statement_shows_up_in_close_checks_over_http(api_small):
    seed_window_http(api_small)
    aid = cash_id(api_small)
    before = api_small.json("/close-checks?through=2026-03-31")
    gate = next(g for g in before["checks"] if g["check"] == "statement_coverage")
    assert gate["status"] == "fail"
    assert any(o["detail"]["account_path"] == CASH and o["kind"] == "statement_not_loaded"
               for o in gate["outstanding"])
    created = api_small.post("/statements", key=uuid.uuid4().hex,
                             json=wire_statement(aid, closing="0", lines=[]))
    assert created.status_code == 201, created.text
    after = api_small.json(created.json()["statement"]["links"]["close_checks"])
    gate = next(g for g in after["checks"] if g["check"] == "statement_coverage")
    outstanding = {(o["kind"], o["detail"]["account_path"], o["detail"]["month"]) for o in gate["outstanding"]}
    # this entity has never been closed, so the window starts at its earliest posted entry (February 2026);
    # March 2026 is the month the statement covers, and it is the one that stops being outstanding.
    assert ("statement_not_loaded", CASH, "2026-03") not in outstanding, "the loaded statement did not register"
    assert ("closing_balance_not_asserted", CASH, "2026-03") not in outstanding
    assert ("statement_not_loaded", CASH, "2026-02") in outstanding, "an unrelated month must stay outstanding"
    assert ("statement_not_loaded", EUR, "2026-03") in outstanding
    assert gate["status"] == "fail"


def test_reading_statements_never_writes(api_small):
    seed_window_http(api_small)
    aid = cash_id(api_small)
    api_small.post("/statements", key=uuid.uuid4().hex, json=wire_statement(aid))
    sid = api_small.scalar("SELECT id FROM statements")
    row = api_small.scalar("SELECT id FROM statement_lines")
    con = sqlite3.connect(api_small.path)
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        before = {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()
    for path in ("/statements", f"/statements/{sid}", f"/statements/{sid}/lines",
                 f"/provenance?line_id={row}", "/close-checks?through=2026-03-31"):
        assert api_small.get(path).status_code == 200, path
    con = sqlite3.connect(api_small.path)
    try:
        after = {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()
    assert after == before


def test_the_write_goes_through_write_transaction_and_rolls_back_whole(fx):
    """Belt and braces on "a refusal leaves no trace": a failure after the inserts still leaves nothing."""
    before = fx.counts()
    with pytest.raises(RuntimeError):
        with write_transaction(fx.engine) as conn:
            from haddon.repository.ctx import WriteCtx
            ctx = WriteCtx.open(conn, AGENT)
            st.load_statement(ctx, account_id=fx.acct(CASH), commodity="USD", period_start="2026-03-01",
                                period_end="2026-03-31", opening_micros="0", closing_micros="0", lines=[],
                                document=document("boom"), locator=LOCATOR, reason="rolled back")
            raise RuntimeError("something later in the request failed")
    assert fx.counts() == before


def test_the_statement_tables_hold_no_real_number_after_a_load(fx):
    submit(fx, opening="0", closing="-3000000", lines=[line("2026-03-05", "-3000000")])
    for table, column in (("statement_lines", "amount_micros"), ("statements", "closing_micros"),
                          ("statements", "opening_micros"), ("documents", "size_bytes")):
        assert fx.scalar(f"SELECT count(*) FROM {table} WHERE typeof({column}) <> 'integer'") == 0


def test_a_statement_id_may_be_supplied_and_cannot_be_reused(fx):
    s = submit(fx, opening="0", closing="0", id="stmt-march-2026")
    assert s.id == "stmt-march-2026"
    e = refused(fx, start="2026-04-01", end="2026-04-30", tag="apr", id="stmt-march-2026")
    assert (e.code, e.status) == ("immutable", 409)


def test_a_loaded_statement_is_immutable(fx):
    s = submit(fx, opening="0", closing="-3000000", lines=[line("2026-03-05", "-3000000")])
    with fx.engine.connect() as conn:
        for sql, params in (("UPDATE statements SET period_end='2026-04-30' WHERE id=?", (s.id,)),
                            ("DELETE FROM statements WHERE id=?", (s.id,)),
                            ("UPDATE statement_lines SET amount_micros=-1 WHERE statement_id=?", (s.id,)),
                            ("DELETE FROM statement_lines WHERE statement_id=?", (s.id,)),
                            ("UPDATE documents SET sha256='x' WHERE id=?", (s.document_id,)),
                            ("DELETE FROM documents WHERE id=?", (s.document_id,)),
                            ("DELETE FROM evidence_links WHERE document_id=?", (s.document_id,))):
            with pytest.raises(sa.exc.DBAPIError) as ei:
                conn.execute(sa.text(sql.replace("?", ":p")), {"p": params[0]})
            assert "immutable" in str(ei.value.orig), sql
            conn.rollback()
