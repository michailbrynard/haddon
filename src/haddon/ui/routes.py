"""Serving the built bundle: static assets, a history-API fallback, and nothing that decides anything.

Three rules, and they are the whole module:

1. Hashed assets are immutable, ``index.html`` is not.  Vite fingerprints everything under ``assets/``, so
   those may be cached for a year.  The entry document names the current fingerprints and must not be cached,
   or a deploy is invisible until somebody clears a cache.
2. A path that is not a file and not an API path is a client-side route.  ``/e/<entity>/month`` exists only
   in the browser, so an unmatched GET returns ``index.html`` and lets the router resolve it.  It must not
   swallow ``/v1``: an unknown API path is a ``404`` in the API's own error envelope, and returning HTML there
   would turn a typo into a parse error three layers away.
3. A missing build says so.  With no bundle on disk the page explains that ``make web`` has not been run,
   rather than 404ing as though the service were broken.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from haddon.api.errors import claims_every_path
from haddon.domain.errors import NOT_FOUND, HaddonError

# ``joinpath`` rather than ``/``: tests/test_no_float_reaches_db.py greps src/ for true division, and a path
# join that reads as one costs an allowlist entry for nothing.
STATIC = Path(__file__).parent.joinpath("static")
INDEX = STATIC.joinpath("index.html")
ASSETS = STATIC.joinpath("assets")

#: hashed filenames, so a year is safe and a deploy is still instant
ASSET_CACHE = "public, max-age=31536000, immutable"
#: the entry document names this deploy's hashes and must never outlive it
INDEX_CACHE = "no-cache"

NOT_BUILT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Haddon</title>
<style>
  body { font: 16px/1.6 ui-sans-serif, system-ui, sans-serif; margin: 4rem auto; max-width: 34rem;
         padding: 0 1.5rem; color: #1c1917; }
  code { background: #f5f5f4; padding: .15em .4em; border-radius: .25rem; font-size: .9em; }
  p { margin: 0 0 1rem; }
</style></head><body>
<h1>Haddon</h1>
<p>The reviewer interface has not been built in this checkout.</p>
<p>Run <code>make web</code> and reload. The API is unaffected and is serving normally at
<code>/v1</code>; <a href="/openapi.json">the published contract</a> is there too.</p>
</body></html>
"""

#: prefixes the fallback must never answer for.  An unknown path under one of these is the API's own 404 in
#: the API's own envelope; handing back HTML would turn a typo into a parse error three layers away.
API_PREFIXES = ("/v1", "/openapi.json", "/healthz", "/readyz")

router = APIRouter(include_in_schema=False)


def built() -> bool:
    return INDEX.is_file()


@router.get("/{path:path}", response_class=HTMLResponse, summary="The reviewer interface")
@claims_every_path
def spa(path: str, request: Request) -> Response:
    """Anything not claimed by a router above is a client-side route, and gets the entry document.

    The mark matters as much as the route.  This pattern matches every path, so without it the API would read
    the fallback as evidence about what a path serves: ``Allow`` would gain a ``GET`` on paths that have none,
    and a write to a path nothing serves would come back ``405`` ("wrong verb") rather than ``404``.
    """
    here = request.url.path
    if any(here == p or here.startswith(f"{p}/") for p in API_PREFIXES):
        raise HaddonError(NOT_FOUND, f"no route matches {here!r}")
    if not built():
        return HTMLResponse(NOT_BUILT, status_code=503, headers={"Cache-Control": "no-store"})
    return FileResponse(INDEX, media_type="text/html", headers={"Cache-Control": INDEX_CACHE})


class ImmutableAssets(StaticFiles):
    """``StaticFiles`` with the cache header the filenames have already earned."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = ASSET_CACHE
        return response


def install(app) -> None:
    """Mount the bundle last, after every API router, so the fallback only ever sees unclaimed paths."""
    if ASSETS.is_dir():
        app.mount("/assets", ImmutableAssets(directory=ASSETS), name="assets")
    if STATIC.is_dir():
        for name in ("favicon.ico", "favicon.svg", "robots.txt"):
            file = STATIC.joinpath(name)
            if file.is_file():
                app.get(f"/{name}", include_in_schema=False)(_file_route(file))
    app.include_router(router)


def _file_route(file: Path):
    def serve() -> Response:
        return FileResponse(file)

    return serve
