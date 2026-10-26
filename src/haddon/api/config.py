"""Service configuration: which entity lives in which database file, and which token acts as which actor.

One database file is one entity (``entities.singleton``), so the configuration is a map from entity id to file
path and the service refuses at startup any file whose ``entities`` row names a different id.

Cursor signing keys are configuration too.  A key generated per process invalidates every open pagination cursor
when the service restarts, a restart the caller did not cause and cannot see.  ``cursor_keys`` is a keyring,
newest first: the first key signs and every key verifies, so a key is rotated by prepending the new one and
dropping the old once the cursors it signed have aged out.

Credentials are deliberately simple: a static bearer token maps to one opaque actor string and an explicit list
of allowed entity ids.  The actor is carried verbatim into ``WriteCtx`` and written to every row it touches.
Nothing in this package parses or branches on it, since there is no human/agent distinction in this system
(``docs/design/decisions.md``, "Authorisation"), so a token is never privileged, only scoped.  There is no
identity provider.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from haddon.domain.errors import HaddonError, validation

ENV_CONFIG = "HADDON_CONFIG"
ALL_ENTITIES = "*"
#: What an entity id may be.  Narrow on purpose: the id addresses the entity in every URL and names the file it
#: is provisioned into, so characters that allow a path traversal, a shell surprise or an ambiguous URL are not
#: allowed.  Lower case only, because two ids differing only in case would be one file on a case-insensitive
#: filesystem but two entities in the map.
ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
#: The key id a one-key ``cursor_secret`` is stored under, and the one an unconfigured service invents.
DEFAULT_KEY_ID = "default"
EPHEMERAL_KEY_ID = "ephemeral"


def check_entity_id(value: object, *, path: str = "entity_id") -> str:
    """Return the id if it is valid, else refuse and say what an id may be.  Call before building a path from one."""
    if not isinstance(value, str) or not ENTITY_ID_PATTERN.match(value):
        raise validation("an entity id is 1-63 characters of lower-case letters, digits and hyphens, starting "
                         "with a letter or a digit", path=path, reason="entity_id_shape", value=value)
    return value


@dataclass(frozen=True)
class CursorKey:
    """One cursor-signing key: an id carried in the cursor, and the secret it is signed with."""

    id: str
    secret: str


@dataclass(frozen=True)
class CursorKeyring:
    """The keys this service signs and verifies cursors with, newest first.

    ``active`` signs; ``by_id`` finds the key to verify with, so a cursor signed before a rotation is honoured
    for as long as its key is still listed.  ``durable`` is False only for the keyring an unconfigured service
    invents for itself.  ``/readyz`` reports that case: it works, until the next restart.
    """

    keys: tuple[CursorKey, ...]
    durable: bool = True

    @classmethod
    def of(cls, secret: str, *, key_id: str = DEFAULT_KEY_ID) -> "CursorKeyring":
        return cls((CursorKey(key_id, secret),))

    @classmethod
    def ephemeral(cls) -> "CursorKeyring":
        return cls((CursorKey(EPHEMERAL_KEY_ID, secrets.token_hex(32)),), durable=False)

    @property
    def active(self) -> CursorKey:
        return self.keys[0]

    def by_id(self, key_id: object) -> CursorKey | None:
        return next((k for k in self.keys if k.id == key_id), None)


@dataclass(frozen=True)
class Credential:
    """One static token: the actor it acts as, and the entities it may touch."""

    token: str
    actor: str
    entities: tuple[str, ...]  # ("*",) means every configured entity

    def may_touch(self, entity_id: str) -> bool:
        return ALL_ENTITIES in self.entities or entity_id in self.entities


@dataclass(frozen=True)
class ProxyAuth:
    """A reverse proxy's authenticated user, accepted as a credential.

    A browser cannot present a bearer token, so the reviewer surface is gated by the proxy's password instead,
    and the proxy passes on who it authenticated.  That header becomes an actor string like any other:
    ``{prefix}{user}``, opaque, scoped to explicit entities, parsed by nothing.

    This is off unless configured, and configuring it is a statement about the deployment: "a reverse proxy in
    front of this service sets this header, and no request reaches the service without passing through it".
    Where that is not true, for example a service reachable directly, leave it unset so a forged header is not
    a credential.  ``deploy/haddon.nginx.conf.example`` is the worked example.
    """

    header: str
    entities: tuple[str, ...] = (ALL_ENTITIES,)
    prefix: str = "ui:"
    anonymous: str = "unknown"

    def actor_for(self, user: str | None) -> str:
        """Return ``{prefix}{user}``.  A blank user maps to ``anonymous``, never to an empty actor."""
        return f"{self.prefix}{(user or '').strip() or self.anonymous}"


@dataclass(frozen=True)
class ApiConfig:
    entities: dict[str, Path]
    credentials: tuple[Credential, ...] = ()
    proxy_auth: "ProxyAuth | None" = None
    #: Where registered document bytes are kept, and the only directory ``GET /documents/{id}/content`` will
    #: ever read from (``haddon.api.evidence``).  A ``blob_path`` is a string a caller wrote into a
    #: registration, so honouring it unconfined would make registration a file-read primitive.  Unset (the
    #: default) means this installation serves no document content at all.
    blob_root: Path | None = None
    #: Where a newly provisioned entity's database file is created, and the only directory
    #: ``POST /v1/entities`` will ever write one into.  An entity id is a caller-supplied string that becomes a
    #: file name, so unconfined it would make provisioning a file-write primitive.  The id must be a plain slug
    #: and the file is always ``entity_root/<id>.db``.  Unset (the default) means this installation does not
    #: provision entities, which suits a deployment whose entity map is managed by hand.
    entity_root: Path | None = None
    #: Where an export run over the API writes its journal files, and the only directory
    #: ``GET /exports/{id}/artifacts/{name}`` will ever read one back from.  An export writes to the filesystem,
    #: so the deployment says where.  Unset (the default) means this installation exports only through
    #: ``bin/haddon-export`` on the machine, and the route refuses rather than choosing a directory.  Each entity
    #: gets its own subdirectory (``export_dir_for``), because the directory inside it is named by
    #: ``journal_mappings``, which is book data, and one book's data must not name another book's directory.
    export_root: Path | None = None
    #: The configuration file this was loaded from, or ``None`` for a configuration built in code.  Provisioning
    #: needs it: an entity registered only in this process's map would disappear at the next restart, which is
    #: worse than refusing to create it.
    source: Path | None = None
    cursor_keys: CursorKeyring = field(default_factory=CursorKeyring.ephemeral)
    # Published limits.  /capabilities and the OpenAPI description read these, so the documented and enforced
    # numbers cannot drift apart.
    default_limit: int = 50
    max_limit: int = 200
    event_default_limit: int = 100
    event_max_limit: int = 500

    def path_for(self, entity_id: str) -> Path | None:
        return self.entities.get(entity_id)

    def provisioning_refusal(self) -> HaddonError | None:
        """Return why this installation cannot provision an entity, or ``None`` if it can.

        There are two separate refusals because there are two different things for an operator to fix: no
        ``entity_root`` to put a database file in, or no configuration file to register the entity in.
        """
        if self.entity_root is None:
            return validation("this installation does not provision entities: no entity_root is configured, so "
                              "there is nowhere to create a database file", path="entity_root",
                              reason="entity_root_unset")
        if self.source is None:
            return validation("this service was not started from a configuration file, so a new entity could "
                              "not be registered durably", path="config", reason="config_not_durable")
        return None

    def export_refusal(self) -> HaddonError | None:
        """Return why this installation cannot run an export, or ``None`` if it can.

        There is only one thing to fix: nothing has said where export files go.  Unlike provisioning, no
        configuration file is needed, because an export is recorded in the entity's own database and survives a
        restart without the deployment's map being touched.
        """
        if self.export_root is None:
            return validation("this installation does not export over the API: no export_root is configured, "
                              "so there is nowhere to write the journal files", path="export_root",
                              reason="export_root_unset")
        return None

    def export_dir_for(self, entity_id: str) -> Path:
        """Return the directory one entity's exports go under: ``export_root/<entity_id>``.

        Confined per entity because the directory inside it is named by ``journal_mappings.journal_path``, which
        is book data written by an import.  A shared root would let one book's rows name the directory another
        book's export is published into.  The id is a slug (``check_entity_id``), so it is one path segment and
        cannot climb out.
        """
        if self.export_root is None:  # pragma: no cover - callers check export_refusal first
            raise validation("no export_root is configured", path="export_root", reason="export_root_unset")
        return self.export_root.joinpath(check_entity_id(entity_id))

    def new_entity_path(self, entity_id: str) -> Path:
        """Return where ``entity_id``'s database file goes.  ``entity_root`` confines it; the id is a slug."""
        if self.entity_root is None:  # pragma: no cover - callers check provisioning_refusal first
            raise validation("no entity_root is configured", path="entity_root", reason="entity_root_unset")
        return self.entity_root.joinpath(f"{entity_id}.db")


def _cursor_keys(raw: Any, secret: Any) -> CursorKeyring | None:
    """Parse the keyring from ``cursor_keys`` (newest first) or the one-key ``cursor_secret``; None if neither."""
    if raw is not None and secret is not None:
        raise validation("give cursor_keys or cursor_secret, not both", path="cursor_keys", reason="conflict")
    if raw is None:
        if secret is None:
            return None
        if not isinstance(secret, str) or not secret:
            raise validation("cursor_secret must be a non-empty string", path="cursor_secret", reason="required")
        return CursorKeyring.of(secret)
    if not isinstance(raw, list) or not raw:
        raise validation("cursor_keys must be a non-empty list of {id, secret}, newest first", path="cursor_keys",
                         reason="list_required")
    keys: list[CursorKey] = []
    seen: set[str] = set()
    for i, spec in enumerate(raw):
        if not isinstance(spec, dict):
            raise validation(f"cursor_keys[{i}] must be an object of {{id, secret}}", path=f"cursor_keys.{i}",
                             reason="object_required")
        kid, value = spec.get("id"), spec.get("secret")
        if not isinstance(kid, str) or not kid:
            raise validation(f"cursor_keys[{i}].id is required", path=f"cursor_keys.{i}.id", reason="required")
        if not isinstance(value, str) or not value:
            raise validation(f"cursor_keys[{i}].secret is required", path=f"cursor_keys.{i}.secret",
                             reason="required")
        if kid in seen:
            raise validation(f"cursor_keys[{i}].id {kid!r} is listed twice; a key id identifies one key",
                             path=f"cursor_keys.{i}.id", reason="duplicate", value=kid)
        seen.add(kid)
        keys.append(CursorKey(kid, value))
    return CursorKeyring(tuple(keys))


def _credentials(raw: Any) -> tuple[Credential, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise validation("tokens must be an object of token -> {actor, entities}", path="tokens", reason="object_required")
    out = []
    for token, spec in raw.items():
        if not isinstance(token, str) or not token:
            raise validation("a token must be a non-empty string", path="tokens", reason="required")
        if not isinstance(spec, dict):
            raise validation(f"tokens.{token} must be an object", path=f"tokens.{token}", reason="object_required")
        actor = spec.get("actor")
        if not isinstance(actor, str) or not actor.strip():
            raise validation(f"tokens.{token}.actor is required", path=f"tokens.{token}.actor", reason="required")
        ents = spec.get("entities", [])
        if not isinstance(ents, list) or not all(isinstance(e, str) and e for e in ents):
            raise validation(f"tokens.{token}.entities must be a list of entity ids", path=f"tokens.{token}.entities",
                             reason="list_required")
        out.append(Credential(token=token, actor=actor, entities=tuple(ents)))
    return tuple(out)


def _proxy_auth(raw: Any) -> ProxyAuth | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise validation("proxy_auth must be an object of {header, entities}", path="proxy_auth",
                         reason="object_required")
    header = raw.get("header")
    if not isinstance(header, str) or not header.strip():
        raise validation("proxy_auth.header is required", path="proxy_auth.header", reason="required")
    ents = raw.get("entities", [ALL_ENTITIES])
    if not isinstance(ents, list) or not all(isinstance(e, str) and e for e in ents):
        raise validation("proxy_auth.entities must be a list of entity ids", path="proxy_auth.entities",
                         reason="list_required")
    kwargs: dict[str, Any] = {"header": header.strip(), "entities": tuple(ents)}
    for key in ("prefix", "anonymous"):
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key]:
                raise validation(f"proxy_auth.{key} must be a non-empty string", path=f"proxy_auth.{key}",
                                 reason="required")
            kwargs[key] = raw[key]
    return ProxyAuth(**kwargs)


def _root(raw: Any, base: Path | None, *, key: str, what: str) -> Path | None:
    """Resolve a configured directory like a database path: relative to the configuration file that named it.

    It is not created and need not exist at load time.  A root mounted after the service starts is an ordinary
    deployment, and a missing directory is already refused clearly at read time.
    """
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise validation(f"{key} must be a non-empty path to the directory {what}", path=key, reason="required")
    path = Path(raw)
    return path if path.is_absolute() or base is None else base.joinpath(path)


def config_from_dict(raw: dict, *, base: Path | None = None, source: Path | None = None) -> ApiConfig:
    ents = raw.get("entities")
    if not isinstance(ents, dict) or not ents:
        raise validation("entities must be a non-empty object of entity_id -> database path", path="entities",
                         reason="required")
    paths: dict[str, Path] = {}
    for eid, p in ents.items():
        if not isinstance(eid, str) or not eid or not isinstance(p, str) or not p:
            raise validation("entities maps an entity id to a database path", path="entities", reason="required")
        path = Path(p)
        paths[eid] = path if path.is_absolute() or base is None else base.joinpath(path)
    kwargs: dict[str, Any] = {"entities": paths, "credentials": _credentials(raw.get("tokens")),
                              "proxy_auth": _proxy_auth(raw.get("proxy_auth")),
                              "blob_root": _root(raw.get("blob_root"), base, key="blob_root",
                                                 what="document bytes are kept in"),
                              "entity_root": _root(raw.get("entity_root"), base, key="entity_root",
                                                   what="a newly provisioned entity's database file is created in"),
                              "export_root": _root(raw.get("export_root"), base, key="export_root",
                                                   what="an export writes its journal files into"),
                              "source": source}
    keyring = _cursor_keys(raw.get("cursor_keys"), raw.get("cursor_secret"))
    if keyring is not None:
        kwargs["cursor_keys"] = keyring
    for key in ("default_limit", "max_limit", "event_default_limit", "event_max_limit"):
        if key in raw:
            v = raw[key]
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise validation(f"{key} must be a positive integer", path=key, reason="range")
            kwargs[key] = v
    return ApiConfig(**kwargs)


def load_config(path: str | Path | None = None) -> ApiConfig:
    """Load the JSON configuration from ``path``, else ``$HADDON_CONFIG``.

    Relative database paths resolve against the configuration file's own directory, so a config file is
    movable."""
    chosen = path if path is not None else os.environ.get(ENV_CONFIG)
    if not chosen:
        raise validation(f"no configuration: pass a path or set ${ENV_CONFIG}", path="config", reason="required")
    file = Path(chosen)
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise validation(f"configuration {file} does not exist", path="config", reason="not_found") from None
    except json.JSONDecodeError as exc:
        raise validation(f"configuration {file} is not JSON: {exc}", path="config", reason="json_invalid") from None
    if not isinstance(raw, dict):
        raise validation("configuration must be a JSON object", path="config", reason="object_required")
    return config_from_dict(raw, base=file.parent, source=file)


def register_entity(config: ApiConfig, entity_id: str, path: Path) -> None:
    """Add ``entity_id -> path`` to the configuration file this service was started from.

    The entity map is the deployment's own statement of which books exist, so it is written back rather than
    held in memory.  An entity only this process knows about would be gone at the next restart.

    Only the ``entities`` object changes.  The file is re-read here rather than re-serialised from
    :class:`ApiConfig`, so tokens, keys and anything else an operator has since edited by hand are preserved.
    The new file is written to a temporary file in the same directory and renamed over the original, which is
    atomic on POSIX: a reader never sees a half-written configuration, and a crash mid-write leaves the
    previous one intact.
    """
    source = config.source
    if source is None:  # pragma: no cover - callers check provisioning_refusal first
        raise validation("this service was not started from a configuration file", path="config",
                         reason="config_not_durable")
    raw = json.loads(source.read_text(encoding="utf-8"))
    entities = raw.get("entities")
    if not isinstance(entities, dict):
        raise validation(f"configuration {source} no longer has an entities object to register into",
                         path="entities", reason="object_required")
    if entity_id in entities:
        raise validation(f"configuration {source} already maps {entity_id!r}", path="entities",
                         reason="duplicate", value=entity_id)
    # Relative to the configuration file where possible, so the deployment stays movable as ``load_config``
    # promises; absolute when the file lives outside the configuration's directory.
    try:
        written = str(path.relative_to(source.parent))
    except ValueError:
        written = str(path)
    entities[entity_id] = written
    directory = source.parent
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=f".{source.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, source.stat().st_mode & 0o7777)  # the configuration holds tokens: keep its own mode
        os.replace(tmp, source)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
