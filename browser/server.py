"""Serving the bundle to a browser the way the deployment does, and proving it is the bundle just built.

It solves two problems.

The entrances.  `deploy/haddon.nginx.conf.example` puts one service behind three locations.  `/v1` takes a bearer
token and has the user header blanked; `/ui/v1` takes the identity the reverse proxy established, passes on
who it authenticated and strips its own prefix so the service sees an ordinary `/v1` request; everything else is
the bundle, behind the same gate.  The page only ever calls `/ui/v1` (`web/src/lib/api.ts`), so a harness that served the app
without that rewrite would send the browser at `/ui/v1/...`, hit the SPA fallback, get `index.html` back and
fail with a JSON parse error three layers from the cause.  `ProxyEntrances` is those three locations as ASGI,
and `assert_matches_deployment` re-reads the nginx file every run so the two cannot drift apart quietly.

The staleness trap.  The bundle is a gitignored build artefact.  A browser test run against a bundle built
before the change under test passes and means nothing, which is worse than no test because it reports green.
`assert_bundle_is_fresh` refuses to serve a bundle older than any file it is built from, and the test asserts
the document the browser actually loaded names the asset that is on disk right now.  `make browser-check`
depends on `make web` as well, so the ordinary path cannot get this wrong in the first place.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from haddon.api.config import ApiConfig, CursorKeyring, ProxyAuth
from haddon.ui import routes as ui

REPO = Path(__file__).resolve().parent.parent
NGINX_CONF = REPO.joinpath("deploy", "haddon.nginx.conf.example")

#: the header nginx sets from `$remote_user`, and the prefix the service turns it into an actor with
USER_HEADER = "x-haddon-user"
REVIEWER = "reviewer"

#: everything the bundle is built from.  Newer than the build means the build is stale.
SOURCES = ("web/src", "web/index.html", "web/package.json", "web/package-lock.json", "web/vite.config.ts",
           "web/components.json", "web/tsconfig.app.json")


class StaleBundle(AssertionError):
    """The built bundle is older than something it is built from, so a run against it would prove nothing."""


class DeploymentDrift(AssertionError):
    """The nginx configuration no longer says what this harness pretends to be."""


# Freshness
def _newest(path: Path) -> tuple[float, Path]:
    if path.is_file():
        return path.stat().st_mtime, path
    newest = (0.0, path)
    for child in path.rglob("*"):
        if child.is_file():
            newest = max(newest, (child.stat().st_mtime, child), key=lambda pair: pair[0])
    return newest


def assert_bundle_is_fresh() -> Path:
    """The built `index.html`, or an explanation of why running against it would be dishonest."""
    if not ui.built():
        raise StaleBundle(f"no bundle at {ui.STATIC}: run `make web` (or `make browser-check`, which does)")
    built_at = ui.INDEX.stat().st_mtime
    for name in SOURCES:
        source = REPO.joinpath(name)
        if not source.exists():
            continue
        when, culprit = _newest(source)
        if when > built_at:
            raise StaleBundle(
                f"{culprit.relative_to(REPO)} changed after the bundle was built "
                f"({time.ctime(when)} vs {time.ctime(built_at)}). Run `make web`: a browser run against a stale "
                f"bundle passes without testing the change.")
    return ui.INDEX


def asset_of(index_html: str) -> str:
    """The hashed script the entry document names, which is the one the browser will execute."""
    match = re.search(r'src="(/assets/[^"]+\.js)"', index_html)
    if match is None:
        raise StaleBundle(f"the built index.html names no module script:\n{index_html[:400]}")
    return match.group(1)


# The deployment's shape
def assert_matches_deployment() -> None:
    """Re-read the nginx configuration and refuse to run if the entrances are no longer the ones mirrored here.

    Without this the shim below is a private fiction: it would keep passing while the deployment it claims to
    imitate had moved, and the harness would be testing a service nobody runs.
    """
    if not NGINX_CONF.is_file():
        raise DeploymentDrift(f"{NGINX_CONF} is missing; this harness mirrors it and cannot check itself")
    conf = NGINX_CONF.read_text(encoding="utf-8")
    blocks = dict(re.findall(r"location\s+(\S+)\s*\{(.*?)\n    \}", conf, re.S))
    required = {
        "/ui/v1/": [(r"proxy_pass\s+http://\S+/v1/;", "must strip its own prefix onto /v1/"),
                    (r"proxy_set_header\s+X-Haddon-User\s+\$remote_user;",
                     "must pass on who the proxy authenticated")],
        "/v1/": [(r"proxy_set_header\s+X-Haddon-User\s+\"\";", "must blank the user header")],
    }
    problems = []
    for location, rules in required.items():
        body = blocks.get(location)
        if body is None:
            problems.append(f"location {location} is gone")
            continue
        for pattern, why in rules:
            if not re.search(pattern, body):
                problems.append(f"location {location} {why}")
    if problems:
        raise DeploymentDrift(f"{NGINX_CONF.name} no longer matches the entrances this harness serves:\n  "
                              + "\n  ".join(problems))


class ProxyEntrances:
    """`deploy/haddon.nginx.conf.example` as ASGI: the `/ui/v1` rewrite, and the blanking on `/v1`.

    The sign-in itself is not modelled: a harness completing the proxy's login would be testing the proxy, not
    the page.  What is modelled is the consequence: which entrance a request came through
    decides whether it carries a user, and the browser only ever uses one of them.
    """

    def __init__(self, app, *, user: str = REVIEWER) -> None:
        self.app = app
        self.user = user

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith("/ui/v1/"):
                scope = self._rewritten(scope, path[len("/ui"):], self.user)
            elif path == "/v1" or path.startswith("/v1/"):
                scope = self._rewritten(scope, path, "")
        await self.app(scope, receive, send)

    @staticmethod
    def _rewritten(scope, path: str, user: str):
        scope = dict(scope)
        scope["path"] = path
        scope["raw_path"] = path.encode("utf-8")
        headers = [(k, v) for k, v in scope["headers"] if k.lower() != USER_HEADER.encode()]
        if user:
            headers.append((USER_HEADER.encode(), user.encode("utf-8")))
        scope["headers"] = headers
        return scope


# The server
@dataclass
class Served:
    base_url: str
    asset: str
    index_html: str


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Harness:
    """A real uvicorn on a real port, so the browser is talking HTTP to the application, not to a test client."""

    def __init__(self, entities: dict[str, Path], *, blob_root: Path | None = None,
                 export_root: Path | None = None, user: str = REVIEWER) -> None:
        from haddon.api import create_app

        assert_matches_deployment()
        self.index = assert_bundle_is_fresh()
        self.index_html = self.index.read_text(encoding="utf-8")
        self.asset = asset_of(self.index_html)
        # The deployment configures a cursor key, a blob root and an export root, so the harness does too.  A
        # fixed cursor key rather than the ephemeral default keeps a cursor working across a restart.  Without
        # `blob_root` the evidence panel could only refuse, and nobody would notice the reviewer cannot open a
        # document; without `export_root` the reports page's export button could only refuse.
        config = ApiConfig(entities=dict(entities), blob_root=blob_root, export_root=export_root,
                           proxy_auth=ProxyAuth(header=USER_HEADER, prefix="ui:"),
                           cursor_keys=CursorKeyring.of("browser-harness-cursor-secret"))
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(ProxyEntrances(create_app(config), user=user),
                                                    host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> Served:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("the harness server exited before it began serving")
            if time.monotonic() > deadline:
                raise RuntimeError(f"the harness server did not start on {self.base_url} within 30s")
            time.sleep(0.02)
        return Served(base_url=self.base_url, asset=self.asset, index_html=self.index_html)

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)
