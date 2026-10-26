"""Authentication: a bearer token resolves to one opaque actor string and an explicit entity scope.

There are two refusals:

* no token, or a token nothing matches: ``401 unauthenticated``;
* a valid token addressed at an entity outside its scope: ``403 forbidden``.

The actor is carried verbatim into ``WriteCtx`` and written to ``created_by`` / ``reviewer``.  Nothing here or
downstream inspects its contents.  There is no privileged shape of actor and no human/agent distinction.  This
module answers "who is acting" and "on which entity", never "who may", and the record does the rest
(``docs/design/decisions.md``, "Authorisation: the model records, it does not enforce").
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from haddon.api.config import ALL_ENTITIES, ApiConfig, Credential
from haddon.domain.errors import FORBIDDEN, UNAUTHENTICATED, HaddonError

BEARER = "bearer"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: the opaque actor it acts as, and the entities it is scoped to."""

    actor: str
    entities: tuple[str, ...]

    @property
    def caller(self) -> str:
        """The key ``requests`` dedupes idempotency under.  One credential is one caller."""
        return self.actor

    def visible(self, configured: list[str]) -> list[str]:
        if ALL_ENTITIES in self.entities:
            return list(configured)
        return [e for e in configured if e in self.entities]

    def require(self, entity_id: str) -> None:
        if ALL_ENTITIES in self.entities or entity_id in self.entities:
            return
        raise HaddonError(FORBIDDEN, f"this credential is not scoped to entity {entity_id!r}",
                          details=[{"path": "entity_id", "entity_id": entity_id, "reason": "entity_not_in_scope"}])


def parse_bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != BEARER or not parts[1].strip():
        return None
    return parts[1].strip()


def _match(config: ApiConfig, token: str) -> Credential | None:
    """Check every configured token in constant time: the loop does not stop at a match, nor the compare at a byte."""
    found = None
    for cred in config.credentials:
        if hmac.compare_digest(cred.token, token):
            found = cred
    return found


def authenticate(config: ApiConfig, authorization: str | None, proxy_user: str | None = None) -> Principal:
    """Resolve the caller: a bearer token first, else the reverse proxy's authenticated user.

    A token is the caller stating who it is, checked against the configuration.  The proxy header is the
    deployment stating it, and counts as a credential only when the configuration says a proxy sets it
    (``ApiConfig.proxy_auth``).  Neither is privileged.  Both produce one opaque actor string and one entity
    scope, and nothing downstream can tell them apart.  A person and an agent correcting the same booking leave
    the same shape of record, differing only in the actor.
    """
    token = parse_bearer(authorization)
    if token is None:
        proxy = config.proxy_auth
        if proxy is not None and proxy_user is not None:
            return Principal(actor=proxy.actor_for(proxy_user), entities=proxy.entities)
        raise HaddonError(UNAUTHENTICATED, "a bearer token is required",
                          details=[{"path": "Authorization", "reason": "missing_bearer"}])
    cred = _match(config, token)
    if cred is None:
        raise HaddonError(UNAUTHENTICATED, "the bearer token is not recognised",
                          details=[{"path": "Authorization", "reason": "unknown_token"}])
    return Principal(actor=cred.actor, entities=cred.entities)
