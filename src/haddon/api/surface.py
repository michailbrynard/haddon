"""What this service actually serves, read off the routes it mounted.

Discovery used to be a hand-written list of paths in ``routes/entity.py`` that had to be kept in step with the
routers by hand (issue 0008 section A asked for this; issue 0006 owns it).  Such a list drifts both ways: a route
added without updating it is a capability nobody is told about, and a route deleted without updating it is
advertised but answers 404.  Both happened.

So the surface is derived from ``app.routes``, the routers actually included.  That already reflects the
extension switches (``haddon.api.app`` does not mount the reconciliation routers when the extension is off).
The mounted route is the declaration, so there is nothing to keep in step.

A write is a command.  Every mutating route declares ``operation_id``, and that string is the command's name in
``/capabilities``, in the OpenAPI document's ``operationId``, and on the function in ``haddon.commands`` /
``haddon.reconciliation_commands`` that does the work.  An agent-tool adapter generating tools from the OpenAPI
document therefore gets the vocabulary this service publishes, not a second one.  A mutating route with no
``operation_id`` would publish FastAPI's generated identifier instead; a test refuses that.

Reads are derived too.  A read is not a command and has no name beyond its path, but "these are the resources
you can inspect" is the other half of discovery, and it drifted the same way.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.routing import APIRoute

V1_PREFIX = "/v1/"
ENTITY_PREFIX = "/v1/entities/{entity_id}"
MUTATING = ("POST", "PUT", "PATCH", "DELETE")


@dataclass(frozen=True)
class Published:
    """One route this service serves, as a caller sees it."""

    method: str
    path: str          # relative to the entity, e.g. "/entries/{entry_id}/reverse"
    command: str | None  # the operation id of a write; None for a read


def _walk(routes) -> list[APIRoute]:
    """Return every ``APIRoute`` under a router, however deeply it is nested.

    On current FastAPI, ``include_router`` does not flatten its routes into ``app.routes``.  It appends an
    ``_IncludedRouter`` wrapper holding the included router, so a one-level scan over ``app.routes`` finds only
    the documentation routes.  A scan that quietly found nothing would publish an empty capability list.  Hence
    the recursion, and hence ``tests/test_api_capabilities.py`` asserts the derived set equals the OpenAPI
    document's rather than just being non-empty.
    """
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
            continue
        nested = getattr(route, "routes", None)
        if nested is None:
            included = getattr(route, "original_router", None)
            nested = getattr(included, "routes", ()) if included is not None else ()
        found.extend(_walk(nested or ()))
    return found


def entity_routes(app) -> list[APIRoute]:
    """Return every route this app serves under ``/v1/entities/{entity_id}``."""
    return [r for r in _walk(app.routes) if r.path.startswith(ENTITY_PREFIX)]


def service_routes(app) -> list[APIRoute]:
    """Return every ``/v1`` route this app serves that is not relative to an entity.

    There is one such resource, the one an entity cannot describe: the collection of entities.  It is kept out
    of ``published`` because the two answer different questions ("what can I do to this book" versus "what can
    I do about which books there are"), and every caller of ``published`` is entity-scoped and would silently
    start reporting a command that is not.
    """
    return [r for r in _walk(app.routes)
            if r.path.startswith(V1_PREFIX) and not r.path.startswith(ENTITY_PREFIX)]


def service_published(app) -> tuple[list[Published], list[Published]]:
    """Return the service-level reads and writes, in the same shape and order as ``published``."""
    reads: list[Published] = []
    writes: list[Published] = []
    for route in service_routes(app):
        for method in sorted(route.methods or ()):
            if method in MUTATING:
                writes.append(Published(method, route.path, route.operation_id))
            elif method == "GET":
                reads.append(Published(method, route.path, None))
    return (sorted(reads, key=lambda p: (p.path, p.method)),
            sorted(writes, key=lambda p: (p.path, p.method)))


def published(app) -> tuple[list[Published], list[Published]]:
    """Return the entity-scoped reads and writes this installation serves, each sorted by path then method."""
    reads: list[Published] = []
    writes: list[Published] = []
    for route in entity_routes(app):
        suffix = route.path[len(ENTITY_PREFIX):]
        for method in sorted(route.methods or ()):
            if method in MUTATING:
                writes.append(Published(method, suffix, route.operation_id))
            elif method == "GET":
                reads.append(Published(method, suffix, None))
    return (sorted(reads, key=lambda p: (p.path, p.method)),
            sorted(writes, key=lambda p: (p.path, p.method)))
