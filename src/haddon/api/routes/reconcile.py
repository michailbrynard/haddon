"""``/reconciliation``: what the loaded statements say a cash account held, and what the ledger says.

The read-only face of :mod:`haddon.repository.reconcile`.  It sits beside ``/close-checks`` rather than under
``/reports``: the reports each sum one record over a date range, while this compares two records at a date.
Close eligibility asks a version of the same question as part of a larger one.

``/close-checks`` answers "would a close be refused, and by which gate".  This answers the question somebody
reviewing a month acts on: do these two agree, and if not, by how much.  Both are read-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from haddon.api import schemas
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.registry import entity_context
from haddon.api.wire import entity_base
from haddon.domain.dates import parse_date
from haddon.repository.events import high_water
from haddon.repository.reconcile import reconcile

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["reconciliation"], responses=schemas.ERRORS)


@router.get("/reconciliation", response_model=schemas.Reconciliation,
            summary="Bank against books: what the loaded statements say a cash account held, and what the ledger says")
def get_reconciliation(er: EntityRequest = Depends(entity_request),
                       as_of: str = Query(..., description="the date to compare at (YYYY-MM-DD)"),
                       from_: str | None = Query(None, alias="from",
                                                 description="start of the period whose statement coverage is "
                                                             "reported; defaults to the first of as_of's month")) -> dict:
    """Compare statement and ledger balances at any date, not only where the bank printed a balance.

    Balances are always cumulative to ``as_of``.  ``from`` narrows only the coverage answer ("which days of the
    period I am looking at have no statement loaded").  A balance is not a period figure, so ``from`` never
    changes it.
    """
    parse_date(as_of, path="as_of")
    if from_ is not None:
        parse_date(from_, path="from")
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        report = reconcile(conn, er.entity_id, as_of=as_of, start=from_)
        seq = high_water(conn, er.entity_id)
    base = entity_base(er.entity_id)
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": seq, **report.as_json(),
            "links": {"statements": f"{base}/statements", "lines": f"{base}/lines?to={as_of}",
                      "close_checks": f"{base}/close-checks?through={as_of}",
                      "trial_balance": f"{base}/reports/trial-balance?as_of={as_of}"}}
