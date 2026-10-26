"""Helpers for the repository-layer tests (chunk B), and the synthetic company they run against (chunk G1).

Everything here goes through the real repository API: ``provision_small_entity`` uses ``provision_entity`` /
``add_commodity`` / ``add_account`` (so it is itself a provisioning test) and ``lock_books`` uses the public
``commands.close_period``, which is the only way a period is closed.  Amounts are
integer strings or ``Micros`` only.  A journal is continuous (issue 0002), so there is no book to open.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import Engine

from haddon.db.connection import sqlite_connect
from haddon.domain.micros import Commodity, Micros
from haddon import commands
from haddon.repository import close_checks, closing, entries, provisioning
from haddon.repository.ctx import WriteCtx
from haddon.repository.transaction import write_transaction

HUMAN = "human:reviewer"
AGENT = "agent:alpha"

# The fixture company (chunk G1).
# Haddon is a general bookkeeping ledger, so most of the suite runs against a company that does not exist:
# Fernside Instruments, a small US instrument maker keeping books in USD with EUR and JPY balances beside them.  A
# fictional company keeps the suite from reading as though the ledger were built for one business (chunk G1 part
# (c)).
#
# Nothing in ``src/`` knows this name, this id or this chart, and nothing may.  It is one synthetic company defined
# once, here.  It is deliberately not a company generator: a fixture that can make any company is a second
# implementation of provisioning rather than a thing to test provisioning against.
COMPANY = "Fernside Instruments"
ENTITY_ID = "fernside-instruments"
COUNTRY = "US"
LEGAL_FORM = "Inc"
BASE_CURRENCY = "USD"
#: Base first, then the two it also holds balances in.  JPY is 0-decimal on purpose, so precision is never uniform.
CURRENCIES = (("USD", 2), ("EUR", 2), ("JPY", 0))
RETAINED_PATH = "equity:retained-earnings"


def m(text: str) -> Micros:
    """Micros from the wire form (a base-10 integer string).  Tests never write bare ints or floats."""
    return Micros.parse(text)


@dataclass
class Fixture:
    engine: Engine
    path: Path
    entity_id: str
    accounts: dict[str, provisioning.Account] = field(default_factory=dict)
    commodities: dict[str, Commodity] = field(default_factory=dict)

    @contextmanager
    def tx(self, actor: str = AGENT, *, now: str | None = None) -> Iterator[WriteCtx]:
        with write_transaction(self.engine) as conn:
            yield WriteCtx.open(conn, actor, now=now)

    @contextmanager
    def reader(self):
        with self.engine.connect() as conn:
            yield conn

    def acct(self, path: str) -> str:
        return self.accounts[path].id

    def raw(self) -> sqlite3.Connection:
        return sqlite_connect(self.path)

    def q(self, sql: str, params: tuple = ()) -> list[tuple]:
        con = self.raw()
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def scalar(self, sql: str, params: tuple = ()):
        return self.q(sql, params)[0][0]

    def counts(self) -> dict[str, int]:
        """Row count of every user table; the zero-trace assertions compare two of these."""
        con = self.raw()
        try:
            names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
        finally:
            con.close()


#: Fernside's chart.  Small on purpose, since every account here is reached by some test, but a whole little
#: company rather than two accounts and a balance: cash in three currencies, a receivable, a payable, and one
#: expense and one income account.
CHART = (
    # path, type, is_cash, commodities
    ("assets:cash", "asset", True, ["USD"]),
    ("assets:cash-eur", "asset", True, ["EUR"]),
    ("assets:cash-jpy", "asset", True, ["JPY"]),
    ("assets:receivables", "asset", False, ["USD"]),
    ("liabilities:payables", "liability", False, ["USD"]),
    ("expenses:office", "expense", False, ["USD", "EUR"]),
    ("income:sales", "income", False, ["USD"]),
)


def provision_small_entity(engine: Engine, *, country: str = COUNTRY, legal_form: str = LEGAL_FORM,
                           entity_id: str = ENTITY_ID) -> Fixture:
    """Provision Fernside Instruments through the real repository API (so this helper is itself a test of it)."""
    fx = Fixture(engine=engine, path=Path(engine.url.database), entity_id=entity_id)
    with write_transaction(engine) as conn:
        ctx = provisioning.provision_entity(conn, actor=HUMAN, id=entity_id, name=COMPANY, legal_form=legal_form,
                                            country=country, base_currency=BASE_CURRENCY, tax_regime={"vat": "none"})
        for code, precision in CURRENCIES:
            fx.commodities[code] = provisioning.add_commodity(ctx, code=code, precision=precision)
        for path, typ, cash, coms in CHART:
            fx.accounts[path] = provisioning.add_account(ctx, path=path, type=typ, is_cash=cash, commodities=coms)
        fx.accounts[RETAINED_PATH] = provisioning.add_account(ctx, path=RETAINED_PATH, type="equity", is_cash=False,
                                                             commodities=["USD"])
        fx.accounts["equity"] = fx.accounts[RETAINED_PATH]
    return fx


def load_fixture(engine: Engine) -> Fixture:
    """Rebuild a ``Fixture`` for a database that ``provision_small_entity`` already populated (template copies)."""
    fx = Fixture(engine=engine, path=Path(engine.url.database), entity_id="")
    with engine.connect() as conn:
        fx.entity_id = provisioning.read_entity(conn).id
        fx.commodities = provisioning.all_commodities(conn, fx.entity_id)
        fx.accounts = {a.path: a for a in provisioning.all_accounts(conn, fx.entity_id).values()}
        fx.accounts["equity"] = fx.accounts[RETAINED_PATH]
    return fx


def pair(fx: Fixture, amount: str, *, commodity: str = "USD", debit: str = "expenses:office", credit: str = "assets:cash",
         memo: str | None = None, tax_code: str | None = None) -> list[entries.Leg]:
    """A balanced two-leg body: +amount on ``debit``, -amount on ``credit``."""
    a = m(amount)
    return [entries.Leg(fx.acct(debit), commodity, a, memo, tax_code), entries.Leg(fx.acct(credit), commodity, -a, memo, tax_code)]


OVERRIDE_REASON = "test fixture: no statement is loaded, so the reconciliation checks cannot be established"


def blocking_overrides(conn, entity_id: str, through: str, *,
                       reason: str = OVERRIDE_REASON) -> list[close_checks.Override]:
    """One override per check that is blocking a close through ``through``, for a fixture that must get past them.

    It reads the checks and names what it finds, which is the only way to write an override: there is no
    wildcard, and an override naming a check that is not blocking is refused.  So a fixture cannot silently keep
    working after a check changes meaning.  It either names the real blockers or it fails.
    """
    checks = close_checks.close_checks(conn, entity_id, through=through)
    return [close_checks.Override(check=g.key, reason=reason) for g in checks.blocking]


def lock_books(fx: Fixture, through: str, *, reason: str = "month close", actor: str = HUMAN,
               override_reason: str = OVERRIDE_REASON):
    """Advance the period lock the way a caller does: one ``close_period`` command, no pre-created ids.

    Every reconciliation check fails closed with no statement loaded, so a fixture close records an explicit
    override per blocking check, as a real one would.  That is the designed path, not a bypass: the overrides are
    on the close record afterwards, and the tests that care assert on them.
    """
    with fx.tx(actor) as ctx:
        lock = closing.get_lock(ctx.conn, ctx.entity_id)
        overrides = blocking_overrides(ctx.conn, ctx.entity_id, through, reason=override_reason)
        return commands.close_period(ctx, through=through, reason=reason, expected_version=lock.version,
                                     overrides=overrides).lock


def post(fx: Fixture, dt: str, amount: str, *, narration: str = "test posting", origin: str = "manual", actor: str = AGENT,
         **legkw) -> entries.Entry:
    with fx.tx(actor) as ctx:
        return entries.post_entry(ctx, dt=dt, narration=narration, origin=origin, legs=pair(fx, amount, **legkw))
