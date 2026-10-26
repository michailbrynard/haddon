"""Request plumbing: the request id, the authenticated principal, and the resolved entity.

Resolution follows the order the spec's credential table implies, which is also the order that does not leak:

1. 401 when there is no usable credential at all;
2. 404 when the service does not serve that entity id (an id from another deployment, or a typo);
3. 403 when the credential is real but not scoped to this entity.

After that, every read and write is bound to one ``EntityHandle``: one database file holding exactly one entity.
Ids inside the request are resolved against that connection, so an id that exists in another entity's file is
simply not found.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import threading

from fastapi import Depends, Path, Request

from haddon.api.config import ApiConfig, CursorKeyring
from haddon.api.registry import EntityFileMismatch, EntityHandle, Registry
from haddon.api.security import Principal, authenticate
from haddon.domain.errors import INTERNAL_ERROR, HaddonError


@dataclass
class ApiState:
    config: ApiConfig
    registry: Registry
    _verifier_lock: threading.Lock = field(default_factory=threading.Lock)
    _verifier: object = None

    def verifier(self):
        """Return the pinned hledger build, resolved once.

        ``resolve_binary`` re-digests a ~50 MB file, so readiness caches the answer instead of paying for it on
        every probe.  Replacing the binary under a running service needs a restart."""
        from haddon.importing.hledger import resolve_binary
        with self._verifier_lock:
            if self._verifier is None:
                self._verifier = resolve_binary()
            return self._verifier

    def verifier_ok(self) -> bool:
        from haddon.importing.hledger import HledgerBinaryError
        try:
            self.verifier()
        except HledgerBinaryError:
            return False
        return True


def state_of(request: Request) -> ApiState:
    return request.app.state.haddon


def current_principal(request: Request) -> Principal:
    config = state_of(request).config
    proxy = config.proxy_auth
    return authenticate(config, request.headers.get("authorization"),
                        None if proxy is None else request.headers.get(proxy.header))


@dataclass(frozen=True)
class EntityRequest:
    """Everything a route needs: who is acting, on which entity file, with which service configuration."""

    request: Request
    state: ApiState
    principal: Principal
    handle: EntityHandle

    @property
    def entity_id(self) -> str:
        return self.handle.entity_id

    @property
    def config(self) -> ApiConfig:
        return self.state.config

    @property
    def cursor_keys(self) -> CursorKeyring:
        return self.state.config.cursor_keys

    def reader(self):
        return self.handle.reader()


def entity_request(request: Request, entity_id: str = Path(..., description="The entity this path is relative to"),
                   principal: Principal = Depends(current_principal)) -> EntityRequest:
    state = state_of(request)
    try:
        handle = state.registry.handle(entity_id)
    except EntityFileMismatch as exc:
        # Configuration is wrong, not the request: say so without naming the file or the other entity.
        raise HaddonError(INTERNAL_ERROR, f"entity {entity_id!r} is misconfigured and cannot be served") from exc
    principal.require(entity_id)
    return EntityRequest(request=request, state=state, principal=principal, handle=handle)
