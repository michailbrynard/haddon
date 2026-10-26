"""Seeded fixture data for the integrity tests.

One entity `E`, USD and EUR at 2 decimals, a chart with cash/non-cash/archived accounts, a continuous journal, a
period lock, statements and their lines, posted/draft/void/reversal entries, a full
explanation, a partial one, a superseded one, an ignored one, and one row of every remaining table.  `lock()`
closes the books through 2026-02-28 the legitimate way (close record + lock update).
"""

from __future__ import annotations

import sqlite3

from repo_helpers import COMPANY

#: The integrity fixture is the same synthetic company as ``repo_helpers``, under a short opaque id: the matrix
#: names it in several hundred SQL literals and a slug would buy nothing there (chunk G1 part (c)).
E = "E"
NOW = "2026-09-10T00:00:00Z"
BY = "seed"


def ins(c: sqlite3.Connection, table: str, **kw) -> None:
    if table != "entities":
        kw.setdefault("entity_id", E)
    kw.setdefault("created_at", NOW)
    kw.setdefault("created_by", BY)
    cols = ",".join(kw)
    ph = ",".join("?" * len(kw))
    c.execute(f"INSERT INTO {table}({cols}) VALUES({ph})", tuple(kw.values()))


def entry(c, eid, dt, legs, origin="source", status="posted", narration="x"):
    """legs = [(account, commodity, micros)]; walks the guarded path draft -> posted/void."""
    ins(c, "entries", id=eid, dt=dt, narration=narration, origin=origin)
    for i, (acct, com, amt) in enumerate(legs, 1):
        ins(c, "postings", id=f"{eid}.p{i}", entry_id=eid, seq=i, account_id=acct, commodity=com, amount_micros=amt)
    if status != "draft":
        c.execute("UPDATE entries SET status=?, version=version+1 WHERE id=?", (status, eid))


def seed(c: sqlite3.Connection) -> sqlite3.Connection:
    ins(c, "entities", id=E, singleton=1, name=COMPANY, legal_form="Inc", country="US", base_currency="USD",
        tax_regime="{}")
    for code in ("USD", "EUR"):
        ins(c, "commodities", id=f"com-{code}", code=code, precision=2, quantum=10000)
    for aid, typ, cash, arch in (("cash", "asset", 1, 0), ("cash-eur", "asset", 1, 0), ("exp", "expense", 0, 0),
                                 ("noncash", "asset", 0, 0), ("arch", "expense", 0, 1), ("retained", "equity", 0, 0)):
        ins(c, "accounts", id=aid, path=f"path:{aid}", type=typ, is_cash=cash, archived=arch)
    for aid, com in (("cash", "USD"), ("cash-eur", "EUR"), ("exp", "USD"), ("exp", "EUR"), ("noncash", "USD"),
                     ("arch", "USD"), ("retained", "USD")):
        ins(c, "account_commodities", id=f"ac-{aid}-{com}", account_id=aid, commodity=com)
    ins(c, "period_locks", id="lock", singleton=1, through=None)
    ins(c, "documents", id="f1", sha256="s1", filename="a.csv", media_type="text/csv", size_bytes=1, blob_path="/x")
    ins(c, "documents", id="f2", sha256="s2", filename="b.pdf", media_type="application/pdf", size_bytes=1,
        blob_path="/y")
    ins(c, "documents", id="f3", sha256="s3", filename="c.pdf", media_type="application/pdf", size_bytes=1,
        blob_path="/w")
    # Spare documents nothing has loaded.  A statement's document is unique, so the integrity probes that write a
    # replacement (or a second statement) need one of their own.
    for n in (4, 5, 6):
        ins(c, "documents", id=f"f{n}", sha256=f"s{n}", filename=f"spare{n}.csv", media_type="text/csv",
            size_bytes=1, blob_path=f"/spare{n}")
    # Three statements, a month each: January on the USD cash account (which `lock()` later closes), and March
    # on both cash accounts.  Monthly rather than annual on purpose: a close window is a month, and a statement
    # spanning the lock could never be replaced.
    ins(c, "statements", id="s_jan", document_id="f1", account_id="cash", commodity="USD",
        period_start="2026-01-01", period_end="2026-01-31", opening_micros=0, closing_micros=-7_000_000,
        locator="p1", reason="january, as downloaded")
    ins(c, "statements", id="s1", document_id="f2", account_id="cash", commodity="USD",
        period_start="2026-03-01", period_end="2026-03-31", opening_micros=-7_000_000,
        closing_micros=1_000_000, locator="p1", reason="march, as downloaded")
    ins(c, "statements", id="s1e", document_id="f3", account_id="cash-eur", commodity="EUR",
        period_start="2026-03-01", period_end="2026-03-31", opening_micros=0, closing_micros=-12_000_000,
        locator="p1", reason="march, as downloaded")

    def row(rid, dt, amt, n, statement="s1"):
        ins(c, "statement_lines", id=rid, statement_id=statement, line_no=n, source_key=f"k{n}",
            content_hash=f"rh{n}", dt=dt, amount_micros=amt, description="d", reference=None, fields="{}",
            raw="raw")

    row("r1", "2026-03-10", -12_000_000, 1)
    row("r2", "2026-03-11", 50_000_000, 2)
    row("r_jan", "2026-01-20", -7_000_000, 1, statement="s_jan")
    row("r_eur", "2026-03-10", -12_000_000, 1, statement="s1e")
    row("r3", "2026-03-12", -5_000_000, 5)
    # r4 is loaded and deliberately left unexplained: the fault-injection write needs a free line to explain
    row("r4", "2026-03-20", -5_000_000, 6)
    entry(c, "e_posted", "2026-03-10", [("cash", "USD", -12_000_000), ("exp", "USD", 12_000_000)])
    entry(c, "e_posted2", "2026-03-11", [("cash", "USD", 50_000_000), ("exp", "USD", -50_000_000)])
    entry(c, "e_jan", "2026-01-20", [("cash", "USD", -7_000_000), ("exp", "USD", 7_000_000)])
    entry(c, "e_eur", "2026-03-10", [("cash-eur", "EUR", -12_000_000), ("exp", "EUR", 12_000_000)])
    # two spare posted cash legs nothing points at: the integrity probes need allocation targets that are not
    # already spoken for, one of each sign.  ``e_open`` pairs with the unexplained line r4.
    entry(c, "e_spare", "2026-03-11", [("cash", "USD", 5_000_000), ("exp", "USD", -5_000_000)])
    entry(c, "e_open", "2026-03-20", [("cash", "USD", -5_000_000), ("exp", "USD", 5_000_000)])
    entry(c, "e_noncash", "2026-03-10", [("noncash", "USD", -12_000_000), ("exp", "USD", 12_000_000)])
    entry(c, "e_draft", "2026-03-12", [("cash", "USD", -5_000_000), ("exp", "USD", 5_000_000)], status="draft")
    entry(c, "e_draft2", "2026-03-13", [("cash", "USD", -5_000_000), ("exp", "USD", 5_000_000)], status="draft")
    entry(c, "e_jan_draft", "2026-01-25", [("cash", "USD", -1_000_000), ("exp", "USD", 1_000_000)], status="draft")
    entry(c, "e_void", "2026-03-12", [("cash", "USD", -5_000_000), ("exp", "USD", 5_000_000)], status="void")
    entry(c, "e_rev", "2026-03-15", [("cash", "USD", 12_000_000), ("exp", "USD", -12_000_000)], origin="reversal")
    entry(c, "e_rev2", "2026-03-15", [("cash", "USD", 12_000_000), ("exp", "USD", -12_000_000)], origin="reversal")
    entry(c, "e_rev_jan", "2026-01-28", [("cash", "USD", 7_000_000), ("exp", "USD", -7_000_000)], origin="reversal")
    ins(c, "corrections", id="cor1", original_id="e_posted2", mirror_id="e_rev2")
    # r1 is fully explained: one live explanation, one allocation covering the whole line
    ins(c, "explanations", id="x_full", line_id="r1", kind="linked", reason="the same money")
    ins(c, "allocations", id="al1", explanation_id="x_full", line_id="r1", posting_id="e_posted.p1",
        amount_micros=-12_000_000)
    # r2 is explained but only partly allocated, the state gate 3 exists to find
    ins(c, "explanations", id="x_partial", line_id="r2", kind="linked", reason="part of the deposit")
    ins(c, "allocations", id="al2", explanation_id="x_partial", line_id="r2", posting_id="e_posted2.p1",
        amount_micros=20_000_000)
    # r_jan is explained inside what `lock()` later closes, so the locked-period cases have something to bite on
    ins(c, "explanations", id="x_jan", line_id="r_jan", kind="linked", reason="january card payment")
    ins(c, "allocations", id="al_jan", explanation_id="x_jan", line_id="r_jan", posting_id="e_jan.p1",
        amount_micros=-7_000_000)
    # r_eur carries a superseded explanation and its live successor: the append-only chain, with both ends present
    ins(c, "explanations", id="x_old", line_id="r_eur", kind="ignored", reason="thought it was a duplicate")
    ins(c, "explanations", id="x_new", line_id="r_eur", kind="linked", reason="it is the euro card payment",
        supersedes_id="x_old")
    ins(c, "allocations", id="al_eur", explanation_id="x_new", line_id="r_eur", posting_id="e_eur.p1",
        amount_micros=-12_000_000)
    # r3 is explained without being booked
    ins(c, "explanations", id="x_ignored", line_id="r3", kind="ignored", reason="a bank fee we do not book here")
    ins(c, "evidence_links", id="ev1", document_id="f2", entry_id="e_posted", locator="l", caption="c")
    # "booked, awaiting a document": one provisional booking, so the append-only chain has something to test
    ins(c, "document_expectations", id="dx1", entry_id="e_posted", state="awaiting", awaiting="the invoice",
        reason="booked gross meanwhile")
    # one recoverable amount that was not recovered, for the chain and the quantum rule to bite on
    ins(c, "unrecovered_tax", id="ut1", entry_id="e_posted", commodity="USD", amount_micros=10000,
        state="unrecovered", reason="the receipt does not support a claim")
    ins(c, "operations", id="op1", kind="import", state="succeeded", intent="{}", counts="{}", errors="[]", attempts=1, version=1)
    ins(c, "operations", id="op2", kind="import", state="running", intent="{}", counts="{}", errors="[]", attempts=1, version=1)
    ins(c, "requests", id="rq1", caller="agent", intent_key="k1", method="POST", path="/x", request_hash="h", http_status=201, response="{}")
    ins(c, "events", entity_id=E, kind="k", subject_kind="entry", subject_id="e_posted", request_id="rq1")
    ins(c, "snapshots", id="sn1", snapshot_seq=1, state_hash="sh1", through="2026-02-28", manifest="{}", report="{}", checks="{}")
    ins(c, "snapshots", id="sn2", snapshot_seq=2, state_hash="sh2", through="2026-03-31", manifest="{}", report="{}", checks="{}")
    ins(c, "exports", id="x1", snapshot_id="sn1", operation_id="op1", manifest_sha256="m", artifacts="[]", verifier="{}")
    ins(c, "journal_mappings", id="jm1", source_revision="abc", journal_path="j", source_index=1,
        source_status="*", canonical_txn="{}", entry_id="e_posted")
    return c


def lock(c, through="2026-02-28", cid="cr1", kind="close", prior=None, reason="month close", snapshot="sn1", version=2):
    """Close the books the legitimate way: append a close record, then move the lock to it."""
    ins(c, "close_records", id=cid, through=through, prior_through=prior, snapshot_id=snapshot, kind=kind,
        reason=reason, overrides="[]")
    c.execute("UPDATE period_locks SET through=?, version=?, close_id=? WHERE id='lock'", (through, version, cid))


# Expected state of a freshly seeded database; the conftest self-check compares against it so a broken seed
# cannot masquerade as passing tests.
EXPECTED_COUNTS = {
    "entries": 14, "postings": 28, "statements": 3, "statement_lines": 6, "explanations": 6, "allocations": 4,
    "documents": 6, "period_locks": 1, "events": 1, "requests": 1, "corrections": 1,
}
EXPECTED_STATUS = {"e_posted": "posted", "e_draft": "draft", "e_void": "void", "e_jan_draft": "draft", "e_rev": "posted"}
