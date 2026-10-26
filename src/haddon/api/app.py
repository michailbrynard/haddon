"""``create_app(config)``: the ASGI application.

The assembly order matters, so it is spelled out here:

1. ``errors.install``.  Every exception, from any layer, leaves as the same envelope, so nothing below needs to
   know how to format a refusal.
2. ``idempotency.install``.  Stamps the request id and refuses any mutating ``/v1`` request without an
   ``Idempotency-Key``.  It is middleware rather than a per-route dependency so that a mutating route added
   later is covered without anyone having to remember.
3. The routers, service-level first.
4. The reviewer UI (``haddon.ui``), outside ``/v1`` and outside the OpenAPI document.  It is a built single-page
   bundle with no application logic: everything it reads and writes goes through ``/v1``, the same surface an
   agent uses.  It goes last because its fallback route claims every path the API did not, which is how a
   client-side route like ``/e/<entity>/month`` survives a page reload.  It authenticates differently: ``/v1``
   carries the agent's bearer token, while the browser is gated by the reverse proxy's password and the proxy's
   authenticated user becomes an actor (``ApiConfig.proxy_auth``).  Every path this app serves is behind one
   of the two.

The OpenAPI document describes what the service enforces.  The limits in its description come from the same
``ApiConfig`` that ``page_limit`` checks, and every route declares a response model that FastAPI validates on
the way out.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from haddon import extensions
from haddon.api import errors, idempotency
from haddon.api.config import ApiConfig
from haddon.api.deps import ApiState
from haddon.api.registry import Registry
from haddon.api.routes import (accounts, artifacts, batches, close, documents, entity, entries, events,
                               lines, provisional, reconcile, reports, service, statements, unrecovered)
from haddon.ui import routes as ui

TITLE = "Haddon"
VERSION = "1"

DESCRIPTION = """\
Haddon: an open-source double-entry ledger for AI agents. It comes with an optional web interface for people.
One database file is one entity; every path below `/v1/entities/{{entity_id}}` is resolved inside that
file, so an id that exists for another entity is simply not found here.

**Amounts** cross the wire as base-10 integer strings of micro-units (millionths), in both directions.
`"amount_micros": "-12000000"` is -12.00 of a two-decimal commodity. JSON numbers are refused.

**Dates** are inclusive `from`/`to` over one continuous journal. Either bound may be omitted (`from` means "from
the beginning", `to` means "through the latest posting"), and any valid range is accepted; `trial-balance` and
`balance-sheet` take a point `as_of` instead. A range that merely ends in the future is fine; one that has not
begun is refused.

**Pagination** returns `{{items, next_cursor, snapshot_seq}}`. Cursors are opaque, signed and bind the entity,
the filters, the ordering and the snapshot; pass them back unchanged. Default {default_limit} per page, maximum
{max_limit}; events default {event_default_limit}, maximum {event_max_limit}. A cursor whose snapshot is gone is
`409 cursor_expired` with a resync link.

**Writes** require an `Idempotency-Key`. The same key with the same request replays the original status and body
with `Idempotency-Replayed: true`; the same key with a different request is `409 idempotency_conflict`.

**Authorisation** records, it does not enforce. A credential is one opaque actor string scoped to explicit
entities; that string is written to `created_by` and is never parsed. Every write is applied on the caller's own
authority and recorded with its actor and reason; there is no approval step.
"""


def openapi_description(config: ApiConfig) -> str:
    return DESCRIPTION.format(default_limit=config.default_limit, max_limit=config.max_limit,
                              event_default_limit=config.event_default_limit,
                              event_max_limit=config.event_max_limit)


def create_app(config: ApiConfig | None = None) -> FastAPI:
    config = config or ApiConfig(entities={})
    registry = Registry(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        registry.dispose()

    app = FastAPI(title=TITLE, version=VERSION, description=openapi_description(config), lifespan=lifespan)
    app.state.haddon = ApiState(config=config, registry=registry)
    errors.install(app)
    idempotency.install(app)
    # The reconciliation extension is switchable (``haddon.extensions``).  With it off its routes are not
    # mounted at all, so nothing is published that would answer 404.  The core journal, reports and close are
    # unaffected.
    modules = [service, entity, accounts, entries, batches, reports, events, artifacts, close,
               documents, provisional, unrecovered]
    if extensions.enabled(extensions.RECONCILIATION):
        # Importing this module registers the extension's close checks with the core.  Without them a close
        # refuses to run rather than passing on the core check alone (``close_checks.policy``).
        from haddon.repository import close_coverage  # noqa: F401  (imported for its registration)

        # Insert relative to a named module, not a literal index.  A core router added to the list above
        # used to silently move where the extension's routers landed.
        at = modules.index(documents)
        modules[at:at] = [reconcile, statements, lines]
    for module in modules:
        app.include_router(module.router)
    ui.install(app)
    return app
