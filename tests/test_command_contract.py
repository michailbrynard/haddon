"""One command, two transports: the same handler reached over HTTP and called directly must leave the same books.

Issue 0006's sixth acceptance criterion: "One contract test drives each command through the transport and
directly through its application handler."  It guards against the same idea implemented twice and drifting.  A
route that re-derives a leg, writes its own audit action or skips a check the handler runs would pass every
route test in the suite, because the route tests only ever ask the route.

So each case runs the command both ways against two identical fresh entities and compares what the books hold
afterwards: the durable accounting rows, and the audit action the command wrote.  Ids are excluded, since they
are generated per write and carry no meaning; everything else must agree exactly.

``load_statement`` stands for the reconciliation extension here.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from haddon import commands, reconciliation_commands as rc
from haddon.db.connection import make_engine
from haddon.db.migrate import upgrade
from haddon.repository import statements as sm
from haddon.repository.close_checks import Override
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx
from haddon.repository.transaction import write_transaction

ENTITY = "twin-works"


def _initialise(path: Path, actor: str):
    upgrade(path)
    engine = make_engine(path)
    with write_transaction(engine) as conn:
        commands.initialize_entity(conn, actor=actor, id=ENTITY, name="Twin Works Ltd", legal_form="Ltd",
                                   country="US", base_currency="USD", base_precision=2)
    return engine


@pytest.fixture
def twins(fast_dir):
    """Two entity files with identical contents, one served over HTTP and one written to directly.

    The same actor on both sides, because the actor is on every row and two different principals would make
    every comparison below fail for an uninteresting reason.
    """
    from conftest import API_ACTOR, API_TOKEN, build_api

    served, direct = fast_dir / "twin-http.db", fast_dir / "twin-direct.db"
    http_engine = _initialise(served, API_ACTOR)
    direct_engine = _initialise(direct, API_ACTOR)
    config, client = build_api({ENTITY: served})

    class Twins:
        def __init__(self):
            self.client, self.engine = client, direct_engine
            self.base = f"/v1/entities/{ENTITY}"
            self.headers = {"Authorization": f"Bearer {API_TOKEN}"}
            self.actor = API_ACTOR

        def over_http(self, path: str, body: dict):
            response = self.client.post(f"{self.base}{path}", json=body,
                                        headers={**self.headers, "Idempotency-Key": str(uuid.uuid4())})
            assert response.status_code in (200, 201), response.text
            return response.json()

        def get(self, path: str):
            response = self.client.get(f"{self.base}{path}", headers=self.headers)
            assert response.status_code == 200, response.text
            return response.json()

        def directly(self, work):
            with write_transaction(self.engine) as conn:
                return work(WriteCtx.open(conn, self.actor))

    twins = Twins()
    with client:
        yield twins
    http_engine.dispose()
    direct_engine.dispose()


# Comparing the books
#: Columns that are a generated identity or a wall-clock reading rather than an accounting fact.
NOT_COMPARED = {"id", "entry_id", "account_id", "posting_id", "statement_id", "document_id", "correction_id",
                "explanation_id", "original_id", "mirror_id", "replacement_id", "request_id", "close_id",
                "snapshot_id", "prior_statement_id", "replaces_id", "created_at", "rowid", "seq",
                "state_hash", "content_hash", "source_key", "input_hash", "lock_close_id", "line_id",
                "supersedes_id", "evidence_id", "allocation_id"}


def _rows(engine, table) -> list[dict]:
    """A table's accounting content, order-independent.

    Sorted by what is left after the ids are dropped rather than by ``id``.  The two sides generate different
    ids, so ordering by them would compare row 1 of one book against row 3 of the other.
    """
    with engine.connect() as conn:
        found = [dict(r._mapping) for r in conn.execute(sa.select(table))]
    stripped = [{k: v for k, v in row.items() if k not in NOT_COMPARED} for row in found]
    return sorted(stripped, key=lambda row: json.dumps(row, sort_keys=True, default=str))


def assert_same_books(twins, *tables) -> None:
    """Every accounting row the two sides wrote, compared column by column with ids and clocks excluded."""
    served_engine = make_engine(Path(twins.client.app.state.haddon.config.entities[ENTITY]), immediate=False)
    try:
        for table in tables:
            assert _rows(served_engine, table) == _rows(twins.engine, table), table.name
    finally:
        served_engine.dispose()


def _actions(engine) -> list[dict]:
    """Each audit action's kind, subject kind, reason and result, with the generated ids stripped out."""
    with engine.connect() as conn:
        rows = conn.execute(sa.select(t.events).order_by(t.events.c.seq)).all()

    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in NOT_COMPARED}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return [{"kind": r.kind, "subject_kind": r.subject_kind, "reason": r.reason,
             "after": strip(json.loads(r.after) if r.after else None)} for r in rows]


def assert_same_audit_trail(twins) -> None:
    served_engine = make_engine(Path(twins.client.app.state.haddon.config.entities[ENTITY]), immediate=False)
    try:
        assert _actions(served_engine) == _actions(twins.engine)
    finally:
        served_engine.dispose()


# The journal core's commands
def test_create_account_writes_the_same_chart_either_way(twins):
    body = {"path": "expenses:professional-services:audit", "type": "expense", "commodities": ["USD"],
            "is_cash": False, "code": "6230", "reason": "the 2026 engagement letter is signed"}
    twins.over_http("/accounts", body)
    twins.directly(lambda ctx: commands.create_account(
        ctx, path=body["path"], type=body["type"], commodities=body["commodities"], is_cash=body["is_cash"],
        code=body["code"], reason=body["reason"]))
    assert_same_books(twins, t.accounts, t.account_commodities)
    assert_same_audit_trail(twins)


def test_archive_account_retires_the_same_account_either_way(twins):
    twins.over_http("/accounts", {"path": "expenses:retired", "type": "expense", "commodities": ["USD"]})
    twins.directly(lambda ctx: commands.create_account(ctx, path="expenses:retired", type="expense",
                                                       commodities=["USD"]))
    reason = "the studio closed and nothing is booked here again"
    twins.over_http("/accounts/expenses:retired/archive", {"reason": reason, "expected_version": 1})
    twins.directly(lambda ctx: commands.archive_account(ctx, account="expenses:retired", reason=reason,
                                                        expected_version=1))
    assert_same_books(twins, t.accounts, t.account_commodities)
    assert_same_audit_trail(twins)


ENTRY = {"dt": "2026-03-02", "narration": "Invoice 0041 — services, March",
         "reason": "the March invoice was raised on the 2nd", "code": "INV-0041",
         "legs": [{"account": "assets:receivables", "commodity": "USD", "amount": "7316.67"},
                  {"account": "income:sales", "commodity": "USD", "amount": "-7316.67"}]}


def _legs(body) -> list[commands.LegSpec]:
    return [commands.LegSpec(leg["account"], leg["commodity"], leg["amount"]) for leg in body["legs"]]


def test_post_entry_books_the_same_entry_either_way(twins):
    twins.over_http("/entries", ENTRY)
    twins.directly(lambda ctx: commands.post_entry(
        ctx, dt=ENTRY["dt"], narration=ENTRY["narration"], reason=ENTRY["reason"], legs=_legs(ENTRY),
        code=ENTRY["code"]))
    assert_same_books(twins, t.entries, t.postings)
    assert_same_audit_trail(twins)


def test_correct_entry_restates_the_same_way_either_way(twins):
    posted = twins.over_http("/entries", ENTRY)
    direct = twins.directly(lambda ctx: commands.post_entry(
        ctx, dt=ENTRY["dt"], narration=ENTRY["narration"], reason=ENTRY["reason"], legs=_legs(ENTRY),
        code=ENTRY["code"]))
    replacement = {"dt": "2026-03-02", "narration": "Invoice 0041 — services, March",
                   "legs": [{"account": "assets:receivables", "commodity": "USD", "amount": "6156.67"},
                            {"account": "income:sales", "commodity": "USD", "amount": "-6156.67"}]}
    reason = "the invoice reads 6,156.67; the figure was transposed"
    # The mirror's narration is given on both sides.  Left to default it names the original entry's id, which
    # is generated per write, so the two books would differ in a string for no accounting reason.
    mirror = "Reversal of invoice 0041, transposed"
    twins.over_http(f"/entries/{posted['entry']['id']}/correct",
                    {"reason": reason, "narration": mirror, "replacement": replacement})
    twins.directly(lambda ctx: commands.correct_entry(
        ctx, original_id=direct.entry.id, reason=reason, narration=mirror,
        replacement=commands.Replacement(
            dt=replacement["dt"], narration=replacement["narration"], origin=commands.DEFAULT_ORIGIN,
            legs=_legs(replacement))))
    assert_same_books(twins, t.entries, t.postings, t.corrections)
    assert_same_audit_trail(twins)


def test_reverse_entry_mirrors_the_same_way_either_way(twins):
    posted = twins.over_http("/entries", ENTRY)
    direct = twins.directly(lambda ctx: commands.post_entry(
        ctx, dt=ENTRY["dt"], narration=ENTRY["narration"], reason=ENTRY["reason"], legs=_legs(ENTRY),
        code=ENTRY["code"]))
    reason = "the engagement was cancelled before any work was done"
    mirror = "Reversal of invoice 0041, cancelled"
    twins.over_http(f"/entries/{posted['entry']['id']}/reverse", {"reason": reason, "narration": mirror})
    twins.directly(lambda ctx: commands.reverse_entry(ctx, original_id=direct.entry.id, reason=reason,
                                                      narration=mirror))
    assert_same_books(twins, t.entries, t.postings, t.corrections)
    assert_same_audit_trail(twins)


def test_close_and_reopen_move_the_lock_the_same_way_either_way(twins):
    twins.over_http("/entries", ENTRY)
    twins.directly(lambda ctx: commands.post_entry(
        ctx, dt=ENTRY["dt"], narration=ENTRY["narration"], reason=ENTRY["reason"], legs=_legs(ENTRY),
        code=ENTRY["code"]))
    closing = "March is reviewed; nothing else is coming"
    # No statement was loaded, so the reconciliation extension's three checks block.  Overriding them is part
    # of what is tested: an override is part of the close command, and a route that built one differently from
    # the handler would show up in the waiver recorded on the close.
    waived = [{"check": key, "reason": "this entity keeps no bank account; nothing settles through cash"}
              for key in ("lines_explained", "nothing_partial", "statement_coverage")]
    twins.over_http("/close", {"through": "2026-03-31", "reason": closing, "expected_version": 1,
                               "overrides": waived})
    twins.directly(lambda ctx: commands.close_period(
        ctx, through="2026-03-31", reason=closing, expected_version=1,
        overrides=[Override(check=o["check"], reason=o["reason"]) for o in waived]))
    reopening = "an invoice dated in March arrived in May"
    twins.over_http("/reopen", {"through": "2026-02-28", "reason": reopening, "expected_version": 2})
    twins.directly(lambda ctx: commands.reopen_period(ctx, through="2026-02-28", reason=reopening,
                                                      expected_version=2))
    assert_same_books(twins, t.period_locks, t.close_records)
    assert_same_audit_trail(twins)


# The reconciliation extension's commands
STATEMENT = {"account": "assets:cash", "commodity": "USD", "period_start": "2026-03-01",
             "period_end": "2026-03-31", "opening": "0", "closing": "-30.00",
             "lines": [{"dt": "2026-03-05", "description": "NORTHWIND CARD 4821", "amount": "-30.00",
                        "currency": "USD", "reference": "TX-8891"}],
             "document": {"sha256": "b" * 64, "filename": "2026-03.csv", "media_type": "text/csv",
                          "size_bytes": 2048, "blob_path": "/var/haddon/blobs/2026-03.csv"},
             "locator": "page 2, closing balance", "page": 2,
             "reason": "the March statement, as downloaded"}


def test_load_statement_records_the_same_statement_and_action_either_way(twins):
    """The ``statement.loaded`` action is written by the command, not the route (issue 0006), so the direct
    side records it too."""
    twins.over_http("/statements", STATEMENT)
    twins.directly(lambda ctx: rc.load_statement(
        ctx, account=STATEMENT["account"], commodity=STATEMENT["commodity"],
        period_start=STATEMENT["period_start"], period_end=STATEMENT["period_end"],
        opening=STATEMENT["opening"], closing=STATEMENT["closing"],
        lines=[rc.LineSpec(dt=ln["dt"], description=ln["description"], amount=ln["amount"],
                           currency=ln["currency"], reference=ln["reference"])
               for ln in STATEMENT["lines"]],
        document=sm.Document(**STATEMENT["document"]), locator=STATEMENT["locator"], page=STATEMENT["page"],
        reason=STATEMENT["reason"]))
    assert_same_books(twins, t.statements, t.statement_lines, t.documents)
    assert_same_audit_trail(twins)


def test_book_line_posts_the_same_entry_either_way(twins):
    loaded = twins.over_http("/statements", STATEMENT)
    direct = twins.directly(lambda ctx: rc.load_statement(
        ctx, account=STATEMENT["account"], commodity=STATEMENT["commodity"],
        period_start=STATEMENT["period_start"], period_end=STATEMENT["period_end"],
        opening=STATEMENT["opening"], closing=STATEMENT["closing"],
        lines=[rc.LineSpec(dt=ln["dt"], description=ln["description"], amount=ln["amount"],
                           currency=ln["currency"], reference=ln["reference"])
               for ln in STATEMENT["lines"]],
        document=sm.Document(**STATEMENT["document"]), locator=STATEMENT["locator"], page=STATEMENT["page"],
        reason=STATEMENT["reason"]))
    served_line = twins.get(f"/statements/{loaded['statement']['statement_id']}/lines")["items"][0]["line_id"]
    with twins.engine.connect() as conn:
        direct_line = conn.execute(sa.select(t.statement_lines.c.id).where(
            t.statement_lines.c.statement_id == direct.statement.id)).scalar_one()

    booking = {"narration": "Office supplies, March", "reason": "the memo names a stationery shop",
               "legs": [{"account": "expenses:general", "commodity": "USD", "amount": "30.00"}]}
    twins.over_http(f"/lines/{served_line}/book", booking)
    twins.directly(lambda ctx: rc.book_line(
        ctx, line_id=direct_line, legs=[commands.LegSpec("expenses:general", "USD", "30.00")],
        narration=booking["narration"], reason=booking["reason"]))
    assert_same_books(twins, t.entries, t.postings, t.explanations, t.allocations)
    assert_same_audit_trail(twins)
