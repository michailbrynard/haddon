"""``/v1/entities/{e}/capabilities``: what this credential may do here, and every published limit.

The reads, writes and named commands are derived from the routes this process actually mounted
(``haddon.api.surface``), not from a list kept beside them.  With the reconciliation extension off its routers
are never included, so its commands do not appear here, and a route added or deleted anywhere changes this
answer automatically.  That is issue 0006's half of discovery; a hand-maintained list had already drifted in
both directions.

"Permission-aware" means what it can in a model that records authority rather than enforcing it
(``decisions.md``, "Authorisation").  The surface is scoped to this entity, which the credential must be scoped
to in order to reach this route, and ``entities_in_scope`` names the others it may address.  There is no role
model to filter by.

Limits are published from the configuration object so the advertised number and the enforced number are the
same.  ``limits.max_limit`` is what ``pagination.page_limit`` refuses above; ``amount_contract`` is what
``wire.reject_wire_numbers`` enforces.

``actor_recorded_as`` is the one place the actor string is shown back to its owner.  It is what will appear in
``created_by`` and ``reviewer`` on everything this credential writes, and it is reported, not interpreted.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends

from haddon.api import schemas, surface
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.registry import entity_context, entity_summary
from haddon.api.wire import entity_base
from haddon.repository import tables as t
from haddon.commands import MAX_BATCH_ENTRIES
from haddon.repository.entries import MAX_LEGS
from haddon.repository.events import high_water

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["entity"], responses=schemas.ERRORS)


@router.get("", response_model=schemas.EntitySummary,
            summary="One entity: who it is, what it holds and how far it is closed")
def entity(er: EntityRequest = Depends(entity_request)) -> dict:
    """The entity itself, in the same shape ``GET /v1/entities`` lists.

    This is where every entity response's ``links.self`` points.  It is built by the same code as the listing's
    items, so the two cannot disagree about what an entity is.
    """
    with er.reader() as conn:
        return entity_summary(conn, verifier_ok=er.state.verifier_ok())


@router.get("/capabilities", response_model=schemas.Capabilities,
            summary="Permissions, published limits, the amount contract and the account/currency bindings")
def capabilities(er: EntityRequest = Depends(entity_request)) -> dict:
    cfg = er.config
    reads, writes = surface.published(er.request.app)
    service_reads, service_writes = surface.service_published(er.request.app)
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        seq = high_water(conn, er.entity_id)
        commodities = [{"code": c, "precision": p, "quantum": q} for c, p, q in conn.execute(
            sa.select(t.commodities.c.code, t.commodities.c.precision, t.commodities.c.quantum)
            .where(t.commodities.c.entity_id == er.entity_id).order_by(t.commodities.c.code))]
        bindings = [{"account_id": a, "commodity": c} for a, c in conn.execute(
            sa.select(t.account_commodities.c.account_id, t.account_commodities.c.commodity)
            .where(t.account_commodities.c.entity_id == er.entity_id)
            .order_by(t.account_commodities.c.account_id, t.account_commodities.c.commodity))]
    base = entity_base(er.entity_id)
    return {
        "entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
        "base_currency": entity["base_currency"], "snapshot_seq": seq,
        "actor_recorded_as": er.principal.actor,
        "entities_in_scope": er.principal.visible(er.state.registry.configured_ids()),
        "reads": [f"{base}{r.path}" for r in reads],
        "writes": [f"{w.method} {base}{w.path}" for w in writes],
        "commands": [{"name": w.command, "method": w.method, "path": f"{base}{w.path}"} for w in writes],
        # The surface that is not relative to an entity, derived the same way.  It is only the entity
        # collection, but without it `initialize_entity`, the command that brings a book into being, could only
        # be found by reading the OpenAPI document by hand.
        "service_reads": [r.path for r in service_reads],
        "service_commands": [{"name": w.command, "method": w.method, "path": w.path} for w in service_writes],
        "limits": {"default_limit": cfg.default_limit, "max_limit": cfg.max_limit,
                   "event_default_limit": cfg.event_default_limit, "event_max_limit": cfg.event_max_limit,
                   "max_entry_legs": MAX_LEGS,
                   # Published because it is enforced: a larger batch is a 422 naming the number, not a
                   # transaction that holds the writer for as long as the caller cares to make it.
                   "max_batch_entries": MAX_BATCH_ENTRIES},
        "amount_contract": {
            "inbound": "exact decimal string in whole commodity units, converted by the commodity's declared "
                       "precision; more decimal places than the currency has is refused, never rounded",
            "inbound_example": "-12.00",
            "outbound": "base-10 integer string of micro-units (millionths); 1 USD is 1000000",
            "outbound_example": "-12000000",
            # Every clause below is something `Micros.from_decimal` / `Micros.parse` actually refuse.  It is this
            # long because of the 0006 review: the shorter wording claimed leading zeros and '-0' were refused
            # while "0012.00", "-0.00", "12.34\n" and Arabic-Indic digits all posted 201.
            "refused": "JSON numbers of any kind, floats, and anything outside signed 64 bits. Both string "
                       "forms are canonical, so each amount has exactly one spelling: ASCII digits 0-9 only "
                       "(no other Unicode decimal digits), no leading '+', no leading zeros ('0012.00'), no "
                       "negative zero ('-0', '-0.00'), and no surrounding whitespace -- including a trailing "
                       "newline"},
        "commodities": commodities, "account_commodities": bindings,
        "idempotency": {"header": "Idempotency-Key", "required_on": sorted({w.method for w in writes}),
                        "hash_binds": ["method", "path", "canonical body", "caller", "entity"],
                        "replay_header": "Idempotency-Replayed",
                        "conflict": "409 idempotency_conflict when the same key carries a different request"},
        "pagination": {"shape": ["items", "next_cursor", "snapshot_seq"], "order": "(created_at, id)",
                       "register_order": "(dt, entry_id, posting_seq)",
                       "cursors": "opaque and signed; they bind entity, filters, ordering and snapshot",
                       "expired": "409 cursor_expired with a resync link"},
        "dates": {"range": "inclusive from/to", "scope": "any valid date range over one continuous journal",
                  "open": "either bound may be omitted: from the beginning, or through the latest posting"},
    }
