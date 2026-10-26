"""``/events``: the append-only log, in order, with a continuation point and the high-water mark.

A follower asks for everything after sequence N.  Two things can go wrong with that, and both are reported here
rather than left for the caller to notice:

* ``since`` beyond the high-water mark means the caller holds a sequence this log does not contain.  It read
  from a database that has since been restored from an earlier backup, or from a different file.  An empty page
  would leave it waiting forever, so this is ``409 event_gap`` with a resync URL.
* ``since`` below the lowest sequence still present means the events in between were pruned and the caller
  would silently skip them.  This log never prunes, so the branch is unreachable today.  It is checked anyway:
  one query is cheap, and a follower that loses writes without an error is not.

``next_since`` is the last sequence on the page; ``high_water`` is how far behind the caller still is.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query

from haddon.api import schemas, views
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.pagination import page_limit
from haddon.api.registry import entity_context
from haddon.api.wire import entity_base
from haddon.domain.errors import EVENT_GAP, HaddonError
from haddon.repository import tables as t
from haddon.repository.events import high_water, read_events

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["events"], responses=schemas.ERRORS)


def _gap(entity_id: str, since: int, water: int, reason: str, low: int | None = None) -> HaddonError:
    detail = {"path": "since", "reason": reason, "since": since, "high_water": water,
              "resync": f"{entity_base(entity_id)}/events?since=0"}
    if low is not None:
        detail["lowest_available"] = low
    return HaddonError(EVENT_GAP, "the requested sequence is not available in this log; resync from the link",
                       details=[detail])


@router.get("/events", response_model=schemas.EventPage, summary="Ordered events after a sequence")
def list_events(er: EntityRequest = Depends(entity_request), since: int = Query(0, ge=0),
                limit: int | None = Query(None)) -> dict:
    size = page_limit(limit, default=er.config.event_default_limit, maximum=er.config.event_max_limit)
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        water = high_water(conn, er.entity_id)
        if since > water:
            raise _gap(er.entity_id, since, water, "beyond_high_water")
        lowest = conn.execute(sa.select(sa.func.min(t.events.c.seq))
                              .where(t.events.c.entity_id == er.entity_id)).scalar()
        if lowest is not None and since > 0 and since < lowest - 1:
            raise _gap(er.entity_id, since, water, "pruned", low=int(lowest))
        events = read_events(conn, er.entity_id, since=since, limit=size)
    items = [views.event(e) for e in events]
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": water, "items": items, "since": since,
            "next_since": items[-1]["seq"] if items else since, "high_water": water}
