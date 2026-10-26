"""Provisioning: one entity, commodities 0..6, accounts with allowed commodities and archive versioning.  A
journal is continuous (issue 0002), so there is no book to create and no retained-earnings mapping to verify."""

from __future__ import annotations

import pytest

from haddon.domain.errors import HaddonError
from haddon.repository import provisioning as pv
from haddon.repository.ctx import WriteCtx
from haddon.repository.transaction import write_transaction

from repo_helpers import BASE_CURRENCY, CHART, COMPANY, COUNTRY, ENTITY_ID, HUMAN


def err(excinfo) -> HaddonError:
    e = excinfo.value
    assert isinstance(e, HaddonError)
    return e


# the helper itself
def test_provision_small_entity_builds_the_expected_state(fx):
    assert fx.scalar("SELECT count(*) FROM entities") == 1
    assert fx.q("SELECT id, singleton, name, country, base_currency FROM entities") == \
        [(ENTITY_ID, 1, COMPANY, COUNTRY, BASE_CURRENCY)]
    assert fx.q("SELECT through, version, close_id FROM period_locks") == [(None, 1, None)]
    assert fx.q("SELECT code, precision, quantum FROM commodities ORDER BY code") == [("EUR", 2, 10000), ("JPY", 0, 1000000), ("USD", 2, 10000)]
    assert fx.scalar("SELECT count(*) FROM accounts") == len(CHART) + 1
    assert fx.scalar("SELECT count(*) FROM account_commodities") == sum(len(c[3]) for c in CHART) + 1
    assert "equity:retained-earnings" in fx.accounts and fx.accounts["equity"].path == "equity:retained-earnings"
    assert fx.q("PRAGMA foreign_key_check") == []
    with fx.reader() as conn:
        e = pv.read_entity(conn)
        assert e.base_currency == BASE_CURRENCY
        assert pv.account_by_path(conn, fx.entity_id, "assets:cash").is_cash is True
        assert pv.account_by_path(conn, fx.entity_id, "expenses:office").is_cash is False
        assert pv.is_commodity_allowed(conn, fx.entity_id, fx.acct("expenses:office"), "EUR")
        assert not pv.is_commodity_allowed(conn, fx.entity_id, fx.acct("assets:cash"), "EUR")


# entity
def test_second_entity_is_refused(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            pv.provision_entity(ctx.conn, actor=HUMAN, id="E2", name="Second", legal_form="Inc", country="US", base_currency="USD")
    assert (err(ei).code, err(ei).status) == ("immutable", 409)
    assert fx.scalar("SELECT count(*) FROM entities") == 1


def test_provision_entity_validates_shape(repo_engine):
    with write_transaction(repo_engine) as conn:
        with pytest.raises(HaddonError) as ei:
            pv.provision_entity(conn, actor="", id="E1", name="x", legal_form="Inc", country="US", base_currency="USD")
        assert err(ei).code == "validation"
        with pytest.raises(HaddonError) as ei:
            pv.provision_entity(conn, actor=HUMAN, id="E1", name=" ", legal_form="Inc", country="US", base_currency="USD")
        assert err(ei).code == "validation" and err(ei).details[0]["path"] == "name"
        # positive control on the same connection: the refusals above wrote nothing
        ctx = pv.provision_entity(conn, actor=HUMAN, id="E1", name="ok", legal_form="Inc", country="US", base_currency="USD")
        assert isinstance(ctx, WriteCtx) and ctx.entity_id == "E1"


def test_write_ctx_open_on_unprovisioned_db_is_internal_error(repo_engine):
    with write_transaction(repo_engine) as conn:
        with pytest.raises(HaddonError) as ei:
            WriteCtx.open(conn, HUMAN)
    assert err(ei).code == "internal_error" and err(ei).status == 500


# commodities
@pytest.mark.parametrize("precision", range(0, 7))
def test_commodity_precision_0_to_6_is_accepted(fx, precision):
    with fx.tx(HUMAN) as ctx:
        c = pv.add_commodity(ctx, code=f"X{precision}", precision=precision)
    assert c.quantum == 10 ** (6 - precision)
    assert fx.q("SELECT precision, quantum FROM commodities WHERE code=?", (f"X{precision}",)) == [(precision, 10 ** (6 - precision))]


@pytest.mark.parametrize("precision", [-1, 7, 8, 100, True, "2", 2.0, None])
def test_commodity_precision_outside_0_to_6_is_422_precision(fx, precision):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            pv.add_commodity(ctx, code="BAD", precision=precision)
    assert (err(ei).code, err(ei).status) == ("precision", 422)
    assert fx.scalar("SELECT count(*) FROM commodities WHERE code='BAD'") == 0


def test_duplicate_commodity_code_is_refused(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            pv.add_commodity(ctx, code="USD", precision=2)
    assert err(ei).code == "immutable"


def test_commodity_code_required(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            pv.add_commodity(ctx, code="  ", precision=2)
    assert err(ei).code == "validation"


# accounts
def test_account_needs_at_least_one_commodity(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            pv.add_account(ctx, path="assets:new", type="asset", is_cash=True, commodities=[])
        assert err(ei).code == "validation" and err(ei).details[0]["path"] == "commodities"
        # positive control: the same account with one commodity is accepted
        a = pv.add_account(ctx, path="assets:new", type="asset", is_cash=True, commodities=["USD"])
    assert a.path == "assets:new" and a.version == 1 and a.archived is False
    assert fx.q("SELECT commodity FROM account_commodities WHERE account_id=?", (a.id,)) == [("USD",)]


def test_account_with_unknown_commodity_is_refused(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            pv.add_account(ctx, path="assets:new", type="asset", is_cash=True, commodities=["USD", "GBP"])
    assert err(ei).code == "validation" and err(ei).details[0]["value"] == ["GBP"]
    assert fx.scalar("SELECT count(*) FROM accounts WHERE path='assets:new'") == 0


def test_duplicate_account_path_is_refused(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            pv.add_account(ctx, path="assets:cash", type="asset", is_cash=True, commodities=["USD"])
    assert (err(ei).code, err(ei).status) == ("immutable", 409)


def test_account_type_enum(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            pv.add_account(ctx, path="x:y", type="revenue", is_cash=False, commodities=["USD"])
    assert err(ei).code == "validation" and err(ei).details[0]["path"] == "type"


def test_bind_account_commodity_duplicate_and_unknowns(fx):
    with fx.tx(HUMAN) as ctx:
        cash = fx.acct("assets:cash")
        with pytest.raises(HaddonError) as ei:
            pv.bind_account_commodity(ctx, account_id=cash, commodity="USD")
        assert err(ei).code == "immutable"
        with pytest.raises(HaddonError) as ei:
            pv.bind_account_commodity(ctx, account_id="nope", commodity="USD")
        assert err(ei).code == "account_unknown"
        with pytest.raises(HaddonError) as ei:
            pv.bind_account_commodity(ctx, account_id=cash, commodity="GBP")
        assert err(ei).code == "validation"
        pv.bind_account_commodity(ctx, account_id=cash, commodity="EUR")  # positive control
    assert fx.scalar("SELECT count(*) FROM account_commodities WHERE account_id=? AND commodity='EUR'", (cash,)) == 1


def test_archive_account_version_check(fx):
    sales = fx.acct("income:sales")
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            pv.archive_account(ctx, account_id=sales, expected_version=2)
        assert (err(ei).code, err(ei).status) == ("stale_version", 412)
        assert err(ei).details[0]["current_version"] == 1
        a = pv.archive_account(ctx, account_id=sales, expected_version=1)
        assert a.archived is True and a.version == 2
        with pytest.raises(HaddonError) as ei:
            pv.archive_account(ctx, account_id=sales, expected_version=2)
        assert err(ei).code == "immutable"  # already archived
        with pytest.raises(HaddonError) as ei:
            pv.archive_account(ctx, account_id="ghost", expected_version=1)
        assert err(ei).status == 404
    assert fx.q("SELECT archived, version FROM accounts WHERE id=?", (sales,)) == [(1, 2)]
