"""A rule the reviewer interface's types have to keep: they declare only fields the API serves.

``web/src/lib/types.ts`` is not documentation.  It is what the compiler believes about every payload the
interface reads.  A field declared there that the service does not send is a claim TypeScript will endorse
without hesitation: ``record.decision_id`` type-checks, ships, and is ``undefined`` at the one moment somebody
reads it.  Nothing catches it in between, because ``tsc`` checks the code against the declaration and has no
opinion about whether the declaration is true.  (``Expectation`` and ``UnrecoveredRecord`` both kept a
non-optional ``decision_id`` after migration 0010 dropped the columns, while the API tests asserted it was gone.)

The check runs against a live payload rather than the OpenAPI schema.  Comparing against the published
components looks cheaper and is wrong in both directions: several payloads are shaped in ``api/views.py`` after the response
model (``reversed_by`` is added there), and several interface names coincide with an unrelated component.  A
guard with false positives in it gets an exemption list, and an exemption list is where a real stale field goes
to hide.  The response the service actually sends has no such ambiguity.

Subset, not equality.  An interface that declares fewer fields than the payload is a page that reads part of a
response, which is ordinary and fine.  The failure being guarded is one-directional: a field declared here
that nothing sends.
"""

from __future__ import annotations

import re
from pathlib import Path

from test_api_documents import write_entry
from test_api_provisional import expect
from test_api_unrecovered_tax import record

TYPES = Path(__file__).resolve().parents[1].joinpath("web", "src", "lib", "types.ts")

#: ``export interface Name {`` ... ``}`` at the top level, which is how every type in the file is written
_BLOCK = re.compile(r"^export interface (\w+)(?: extends \w+)? \{\n(.*?)^\}", re.M | re.S)
#: one declared member per line: ``  name: T`` or ``  name?: T``, at the interface's own indent
_MEMBER = re.compile(r"^  (\w+)\??:", re.M)


def declared(name: str) -> set[str]:
    """The field names ``types.ts`` declares on one interface."""
    blocks = {found: body for found, body in _BLOCK.findall(TYPES.read_text())}
    assert name in blocks, f"{name} is no longer declared in {TYPES.name}; this test names a type that is gone"
    fields = set(_MEMBER.findall(blocks[name]))
    assert fields, f"{name} parsed as having no fields, which means the parser stopped matching the file"
    return fields


def test_the_expectation_type_declares_only_fields_the_api_sends(api_small):
    """``decision_id`` was declared here after migration 0010 dropped the column it named."""
    served = set(expect(api_small, write_entry(api_small)).json()["expectation"])
    assert "decision_id" not in served, "the payload changed; this test's premise is stale, not the type"
    assert declared("Expectation") <= served, sorted(declared("Expectation") - served)


def test_the_unrecovered_record_type_declares_only_fields_the_api_sends(api_small):
    """The same dead column, declared on the other table that carried one."""
    served = set(record(api_small, write_entry(api_small, dt="2026-04-27")).json()["record"])
    assert "decision_id" not in served, "the payload changed; this test's premise is stale, not the type"
    assert declared("UnrecoveredRecord") <= served, sorted(declared("UnrecoveredRecord") - served)


def test_the_shaped_statement_types_declare_only_fields_the_api_sends(api_small):
    """The interface's shaped-statement types are a mirror of the served arrangement, and the mirror is checked.

    ``types.ts`` is what the compiler believes about the JSON the screen renders.  A field declared there that
    ``/reports/statements`` does not send type-checks, ships, and is ``undefined`` at the moment somebody reads
    it, and the shape of a statement is exactly where a stale field would misstate money.  Every interface is
    a subset of a real payload, one level at a time, so a rename on either side fails here.
    """
    from test_api_statements_view import post_pair

    # a posting inside the span this period is compared with, so the payload this is checked against carries a
    # comparative: a book whose first period this is has none to declare, and every type below would then be
    # checked against a column that is not there.  Early in the year, because a 2026 request made during 2026
    # is compared with the same dates in 2025 rather than the whole of it.
    post_pair(api_small, "2025-03-20", "1000000", commodity="USD", debit="assets:cash", credit="income:sales")
    post_pair(api_small, "2026-02-04", "100000000", commodity="USD", debit="assets:cash", credit="income:sales")
    post_pair(api_small, "2026-02-10", "30000000", commodity="USD", debit="expenses:office", credit="assets:cash")
    served = api_small.json("/reports/statements?from=2026-01-01&to=2026-12-31")
    assert served["comparative"] is not None, "this test's premise"

    assert declared("StatementsDocument") <= set(served), sorted(declared("StatementsDocument") - set(served))
    assert declared("PeriodLabel") <= set(served["period"])
    assert declared("ComparativeLabel") <= set(served["comparative"])

    per = served["statements"][0]
    assert declared("CommodityStatements") <= set(per)
    for name, payload in (("IncomeStatementView", per["income_statement"]),
                          ("BalanceSheetView", per["balance_sheet"])):
        assert declared(name) <= set(payload), sorted(declared(name) - set(payload))

    section = per["income_statement"]["sections"][0]
    assert declared("ShapedSection") <= set(section)
    group = section["groups"][0]
    assert declared("ShapedGroup") <= set(group)
    assert declared("ShapedLine") <= set(group["lines"][0])
    assert declared("BalanceCheck") <= set(per["balance_sheet"]["checks"][0])
