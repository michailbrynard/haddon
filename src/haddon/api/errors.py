"""Rendering the spec's error envelope, and nothing else.

``src/haddon/domain/errors.py`` owns the code-to-HTTP-status table; this module is the HTTP end of it.  It
guarantees that:

* a ``HaddonError`` raised anywhere beneath (repository, reports, importer, this package) surfaces with its own
  code, status and ``details``.  It is never flattened into a 500 and never reworded.
* an unexpected exception is a ``500 internal_error`` carrying the request id and nothing else: no exception
  text, no SQL, no path, no token.  The detail goes to the server log.
* a refusal Starlette raises itself (an unrouted path, a verb a path does not serve) carries the client-error
  code for its status, not ``internal_error``.  A caller that used the wrong verb has not found a fault in this
  service.
* ``next_action`` is derived from the details the domain already produces (a resync link on an expired cursor or
  an event gap), so the actionable part of an error is worked out once, in the layer that knows why the refusal
  happened.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.routing import Match

from haddon.domain.errors import (CURSOR_EXPIRED, EVENT_GAP, FORBIDDEN, INTERNAL_ERROR, METHOD_NOT_ALLOWED,
                                  NOT_FOUND, STALE_VERSION, UNAUTHENTICATED, VALIDATION, HaddonError)

log = logging.getLogger("haddon.api")

REQUEST_ID_HEADER = "X-Request-Id"

#: Starlette raises ``HTTPException`` for its own refusals before any route runs.  Each status it can produce
#: maps to the code the error table already has for that meaning.  A 4xx with no entry is still the caller's
#: request being wrong (``validation``); only a 5xx is this service's fault.
STARLETTE_CODES = {401: UNAUTHENTICATED, 403: FORBIDDEN, 404: NOT_FOUND, 405: METHOD_NOT_ALLOWED}

#: Set on an endpoint that answers paths it does not define: the reviewer bundle's history-API fallback, which
#: claims every path the API did not.  Such a route says nothing about what a path serves, so it is left out of
#: the ``Allow`` union.  ``include_in_schema`` is not the same question; using it as a proxy left
#: ``/openapi.json`` and the favicons, real routes that are simply unpublished, out of their own answer.
CLAIMS_EVERY_PATH = "haddon_claims_every_path"


def claims_every_path(endpoint):
    """Mark an endpoint as a fallback: it matches paths it does not define, so it does not define their verbs."""
    setattr(endpoint, CLAIMS_EVERY_PATH, True)
    return endpoint


def _is_fallback(route) -> bool:
    return bool(getattr(getattr(route, "endpoint", None), CLAIMS_EVERY_PATH, False))


def _leaf_routes(routes) -> list[Any]:
    """Return the leaf routes, out of whatever nesting the framework holds them in.

    FastAPI keeps an included router as one entry holding its own routes rather than flattening them into
    ``app.routes``, so the top level alone shows routers, not routes.  An included router exposes them as
    ``original_router.routes``, a mount as ``routes``.  Both are framework internals, so
    ``test_the_allowed_methods_are_the_whole_set_and_not_the_first_route_that_matched`` asserts the actual verbs.
    If a FastAPI upgrade moves them, ``Allow`` would otherwise silently go empty.
    """
    out: list[Any] = []
    for route in routes:
        nested = (getattr(route, "routes", None)
                  or getattr(getattr(route, "original_router", None), "routes", None))
        if nested:
            out.extend(_leaf_routes(nested))
        else:
            out.append(route)
    return out


def allowed_methods(request: Request) -> list[str]:
    """Return every method the routes for this path serve, unioned across routes.

    Starlette's own ``Allow`` names only the first route whose path matched, so on a path served by more than one
    route (``/accounts`` is ``GET`` and ``POST``) it is incomplete, and tells a caller the verb they wanted is
    unavailable when it is right there.  Routes marked with ``claims_every_path`` (the reviewer bundle's
    catch-all) are skipped: they match every path the API did not, and define nothing about what it serves.
    """
    methods: set[str] = set()
    for route in _leaf_routes(request.app.routes):
        if _is_fallback(route):
            continue
        match, _ = route.matches(request.scope)
        if match is Match.PARTIAL:
            methods |= set(getattr(route, "methods", None) or ())
    return sorted(methods)


def code_for_status(status: int) -> str:
    code = STARLETTE_CODES.get(status)
    if code is not None:
        return code
    return VALIDATION if 400 <= status < 500 else INTERNAL_ERROR


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "req-unknown")


def next_action_for(exc: HaddonError) -> dict[str, Any] | None:
    """Return the next step the caller should take, read off the details the domain produced, or ``None``."""
    detail = exc.details[0] if exc.details else {}
    if exc.code in (CURSOR_EXPIRED, EVENT_GAP) and detail.get("resync"):
        return {"action": "resync", "url": detail["resync"]}
    if exc.code == STALE_VERSION:
        return {"action": "reload", "reason": "the version you supplied is not the current one"}
    return None


def envelope(exc: HaddonError, request_id: str) -> dict[str, Any]:
    body = exc.envelope(request_id)
    action = next_action_for(exc)
    if action is not None:
        body["error"]["next_action"] = action
    return body


def error_response(exc: HaddonError, request_id: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=envelope(exc, request_id),
                        headers={REQUEST_ID_HEADER: request_id, **(headers or {})})


def install(app: FastAPI) -> None:
    @app.exception_handler(HaddonError)
    async def _haddon(request: Request, exc: HaddonError):
        return error_response(exc, request_id_of(request))

    @app.exception_handler(RequestValidationError)
    async def _unprocessable(request: Request, exc: RequestValidationError):
        details = [{"path": ".".join(str(p) for p in err.get("loc", ())), "reason": err.get("type", "validation"),
                    "message": err.get("msg", "")} for err in exc.errors()]
        wrapped = HaddonError("validation", "the request does not match the published schema", details=details)
        return error_response(wrapped, request_id_of(request))

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        # Starlette's own refusals (an unrouted path, a bad method) come through here so that every response
        # body in the service has the same shape.
        code = code_for_status(exc.status_code)
        if code != METHOD_NOT_ALLOWED:
            return error_response(HaddonError(code, str(exc.detail), status=exc.status_code),
                                  request_id_of(request))
        # An empty union means no route defines this path with any verb; only the reviewer bundle's fallback
        # matched, and a fallback is not a resource.  Starlette says 405 because the fallback's pattern matched.
        # Answer the 404 a GET to the same path already gets.  That also keeps the rule below true: a 405 always
        # carries a non-empty ``Allow``, as RFC 9110 15.5.6 requires.
        allowed = allowed_methods(request)
        if not allowed:
            return error_response(HaddonError(NOT_FOUND, f"no route matches {request.url.path!r}"),
                                  request_id_of(request))
        # 405 carries ``Allow`` as HTTP requires, and the same verbs are repeated in the envelope so a JSON
        # client need not read headers to find out what it may do here instead.
        detail = {"path": request.url.path, "reason": "method_not_allowed", "method": request.method,
                  "allowed": allowed}
        wrapped = HaddonError(METHOD_NOT_ALLOWED,
                              f"{request.method} is not a method this path serves", details=[detail])
        return error_response(wrapped, request_id_of(request), headers={"Allow": ", ".join(allowed)})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        rid = request_id_of(request)
        log.exception("unhandled error serving %s %s (request_id=%s)", request.method, request.url.path, rid)
        return error_response(HaddonError(INTERNAL_ERROR, "the request failed and was rolled back"), rid)
