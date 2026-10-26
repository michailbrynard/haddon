"""The reviewer UI: a built single-page application, served by the same process that serves ``/v1``.

This package holds no application logic at all.  Everything the UI reads and writes goes through ``/v1``,
the same surface an agent uses, so there is one contract and one set of tests behind it rather than a browser
path quietly diverging from the API path.  What is left here is the three things serving a bundle needs: the
static files, the fallback that hands ``index.html`` to a client-side route, and the honest page to show when
nobody has run the build.

There are two credentials and one surface.  ``/v1`` carries the agent's bearer token.  A browser cannot present
one, so the reviewer surface is gated by the reverse proxy's password and the proxy passes on who it
authenticated; ``ApiConfig.proxy_auth`` turns that header into an actor string like any other.  That is a
difference between two credentials, not between two kinds of caller.

The chrome never names an actor type.  This system does not distinguish a human caller from an automated one
(``docs/design/decisions.md`` § "Authorisation"), so a label reading "needs a person" or "the agent booked this"
would assert a difference the code does not make.  It would be wrong besides, since any of these lines can be
resolved by whoever gets to it.  What the UI does show freely is the recorded ``actor`` of something that already
happened, because that is data.  ``tests/test_ui_copy.py`` holds the line, over the frontend sources
as well as this package.
"""

from __future__ import annotations

from haddon.ui.routes import install

__all__ = ["install"]
