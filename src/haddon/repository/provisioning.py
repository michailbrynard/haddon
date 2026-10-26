"""Provisioning: entity, commodities, chart of accounts, allowed commodities per account, and the lock row.

Spec: "Seed entities/lock once before accepting writes"; "explicit allowed currencies for every account".
A journal is continuous (issue 0002), so there is no book to create and no retained-earnings mapping to verify.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.errors import ACCOUNT_UNKNOWN, IMMUTABLE, STALE_VERSION, HaddonError, not_found, validation
from haddon.domain.micros import Commodity, quantum_for
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx, check_actor, utc_now
from haddon.repository.errors import wrap_db_error

ACCOUNT_TYPES = ("asset", "liability", "equity", "income", "expense")


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    legal_form: str
    country: str
    base_currency: str


@dataclass(frozen=True)
class Account:
    id: str
    path: str
    type: str
    code: str | None
    tax_code: str | None
    is_cash: bool
    archived: bool
    version: int


def provision_entity(conn: Connection, *, actor: str, id: str, name: str, legal_form: str, country: str,
                     base_currency: str, tax_regime: dict | None = None, now: str | None = None,
                     request_id: str | None = None) -> WriteCtx:
    """Insert the one entity row and its period-lock row, and return a ``WriteCtx`` for the rest of provisioning."""
    check_actor(actor)
    for path, value in (("id", id), ("name", name), ("legal_form", legal_form), ("country", country), ("base_currency", base_currency)):
        if not isinstance(value, str) or not value.strip():
            raise validation(f"{path} is required", path=path, reason="required")
    if conn.execute(sa.select(t.entities.c.id)).first() is not None:
        raise HaddonError(IMMUTABLE, "entity already provisioned", details=[{"path": "entities"}])
    now = now or utc_now()
    try:
        conn.execute(sa.insert(t.entities).values(
            id=id, singleton=1, name=name, legal_form=legal_form, country=country, base_currency=base_currency,
            tax_regime=json.dumps(tax_regime or {}, sort_keys=True), created_at=now, created_by=actor))
    except sa.exc.DBAPIError as exc:
        raise wrap_db_error(exc) from exc
    # ``request_id`` links the audit action this provisioning writes to the request row that commits beside it.
    # ``WriteCtx.open`` cannot be used because it reads the entity row, so the entity id is passed in directly.
    ctx = WriteCtx(conn=conn, entity_id=id, actor=actor, now=now, request_id=request_id)
    ctx.insert(t.period_locks, "lock", "lock", singleton=1, through=None, version=1, close_id=None)
    return ctx


def read_entity(conn: Connection) -> Entity:
    row = conn.execute(sa.select(t.entities.c.id, t.entities.c.name, t.entities.c.legal_form, t.entities.c.country,
                                 t.entities.c.base_currency)).first()
    if row is None:
        raise validation("entity not provisioned", reason="entity_missing")
    return Entity(*row)


def add_commodity(ctx: WriteCtx, *, code: str, precision: int, id: str | None = None) -> Commodity:
    if not isinstance(code, str) or not code.strip():
        raise validation("commodity code is required", path="code", reason="required")
    quantum = quantum_for(precision)  # raises 422 precision for anything outside 0..6
    if get_commodity(ctx.conn, ctx.entity_id, code) is not None:
        raise HaddonError(IMMUTABLE, f"commodity {code!r} already exists", details=[{"path": "code", "value": code}])
    ctx.insert(t.commodities, id or f"com-{code}", "com", code=code, precision=precision, quantum=quantum)
    return Commodity(code, precision)


def get_commodity(conn: Connection, entity_id: str, code: str) -> Commodity | None:
    row = conn.execute(sa.select(t.commodities.c.code, t.commodities.c.precision)
                       .where(t.commodities.c.entity_id == entity_id, t.commodities.c.code == code)).first()
    return Commodity(row[0], row[1]) if row else None


def all_commodities(conn: Connection, entity_id: str) -> dict[str, Commodity]:
    rows = conn.execute(sa.select(t.commodities.c.code, t.commodities.c.precision).where(t.commodities.c.entity_id == entity_id))
    return {code: Commodity(code, precision) for code, precision in rows}


def add_account(ctx: WriteCtx, *, path: str, type: str, is_cash: bool, commodities: list[str], id: str | None = None,
                code: str | None = None, tax_code: str | None = None) -> Account:
    if not isinstance(path, str) or not path.strip():
        raise validation("account path is required", path="path", reason="required")
    if type not in ACCOUNT_TYPES:
        raise validation(f"account type {type!r} is not one of {ACCOUNT_TYPES}", path="type", reason="enum", value=type)
    if not commodities:
        raise validation("every account declares at least one allowed commodity", path="commodities", reason="required")
    known = all_commodities(ctx.conn, ctx.entity_id)
    unknown = [c for c in commodities if c not in known]
    if unknown:
        raise validation(f"unknown commodities {unknown}", path="commodities", reason="commodity_unknown", value=unknown)
    if ctx.conn.execute(sa.select(t.accounts.c.id).where(t.accounts.c.path == path)).first() is not None:
        raise HaddonError(IMMUTABLE, f"account path {path!r} already exists", details=[{"path": "path", "value": path}])
    aid = ctx.insert(t.accounts, id, "acct", path=path, type=type, code=code, tax_code=tax_code, is_cash=int(bool(is_cash)),
                     archived=0, version=1)
    for c in dict.fromkeys(commodities):
        bind_account_commodity(ctx, account_id=aid, commodity=c)
    return get_account(ctx.conn, ctx.entity_id, aid)  # type: ignore[return-value]


def bind_account_commodity(ctx: WriteCtx, *, account_id: str, commodity: str, id: str | None = None) -> str:
    if get_account(ctx.conn, ctx.entity_id, account_id) is None:
        raise HaddonError(ACCOUNT_UNKNOWN, f"account {account_id!r} not found", details=[{"path": "account_id", "id": account_id}])
    if get_commodity(ctx.conn, ctx.entity_id, commodity) is None:
        raise validation(f"unknown commodity {commodity!r}", path="commodity", reason="commodity_unknown", value=commodity)
    if is_commodity_allowed(ctx.conn, ctx.entity_id, account_id, commodity):
        raise HaddonError(IMMUTABLE, f"{account_id} already allows {commodity}", details=[{"account_id": account_id, "commodity": commodity}])
    return ctx.insert(t.account_commodities, id or f"ac-{account_id}-{commodity}", "ac", account_id=account_id, commodity=commodity)


def is_commodity_allowed(conn: Connection, entity_id: str, account_id: str, commodity: str) -> bool:
    return conn.execute(sa.select(t.account_commodities.c.id).where(
        t.account_commodities.c.entity_id == entity_id, t.account_commodities.c.account_id == account_id,
        t.account_commodities.c.commodity == commodity)).first() is not None


def allowed_commodities(conn: Connection, entity_id: str) -> set[tuple[str, str]]:
    return {(a, c) for a, c in conn.execute(sa.select(t.account_commodities.c.account_id, t.account_commodities.c.commodity)
                                           .where(t.account_commodities.c.entity_id == entity_id))}


def _account_row(row) -> Account:
    return Account(id=row.id, path=row.path, type=row.type, code=row.code, tax_code=row.tax_code, is_cash=bool(row.is_cash),
                   archived=bool(row.archived), version=row.version)


def get_account(conn: Connection, entity_id: str, account_id: str) -> Account | None:
    row = conn.execute(sa.select(t.accounts).where(t.accounts.c.entity_id == entity_id, t.accounts.c.id == account_id)).first()
    return _account_row(row) if row else None


def all_accounts(conn: Connection, entity_id: str) -> dict[str, Account]:
    return {r.id: _account_row(r) for r in conn.execute(sa.select(t.accounts).where(t.accounts.c.entity_id == entity_id))}


def account_by_path(conn: Connection, entity_id: str, path: str) -> Account | None:
    row = conn.execute(sa.select(t.accounts).where(t.accounts.c.entity_id == entity_id, t.accounts.c.path == path)).first()
    return _account_row(row) if row else None


def archive_account(ctx: WriteCtx, *, account_id: str, expected_version: int) -> Account:
    acct = get_account(ctx.conn, ctx.entity_id, account_id)
    if acct is None:
        raise not_found("account", account_id)
    if acct.version != expected_version:
        raise HaddonError(STALE_VERSION, "account changed", details=[{"id": account_id, "current_version": acct.version}])
    if acct.archived:
        raise HaddonError(IMMUTABLE, f"account {account_id!r} is already archived", details=[{"id": account_id}])
    ctx.execute(sa.update(t.accounts).where(t.accounts.c.id == account_id, t.accounts.c.version == expected_version)
                .values(archived=1, version=expected_version + 1))
    return get_account(ctx.conn, ctx.entity_id, account_id)  # type: ignore[return-value]


def restore_account(ctx: WriteCtx, *, account_id: str, expected_version: int) -> Account:
    """The inverse of :func:`archive_account`: put a retired account back in use.

    The storage model always allowed this.  ``accounts_update`` refuses an identity change or a missing version
    bump but does not care which way ``archived`` moves, so this adds a missing command rather than relaxing a rule.
    ``decisions.md`` explains why it exists: without it, archiving an account that still carried a balance, a
    posted entry or an unbooked statement line was a one-way door.
    """
    acct = get_account(ctx.conn, ctx.entity_id, account_id)
    if acct is None:
        raise not_found("account", account_id)
    if acct.version != expected_version:
        raise HaddonError(STALE_VERSION, "account changed", details=[{"id": account_id, "current_version": acct.version}])
    if not acct.archived:
        raise HaddonError(IMMUTABLE, f"account {account_id!r} is not archived", details=[{"id": account_id}])
    ctx.execute(sa.update(t.accounts).where(t.accounts.c.id == account_id, t.accounts.c.version == expected_version)
                .values(archived=0, version=expected_version + 1))
    return get_account(ctx.conn, ctx.entity_id, account_id)  # type: ignore[return-value]
