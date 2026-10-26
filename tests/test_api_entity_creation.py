"""``POST /v1/entities``: creating a set of books over the same surface everything else uses.

Without this route an agent asked to keep a second company's accounts could not start: the book could only be
made by running ``bin/haddon-init`` on the machine and editing the service configuration by hand.

Beyond the happy path, the tests cover the two things that make creation safe to offer over HTTP:

* The id is a caller's string and it becomes a file name.  A path, an upper-case twin of an existing id or a
  traversal is refused before anything is written, and the refusal says what an id may be.
* A half-created book is never addressable.  The file is made, migrated and provisioned before the entity map
  hears about it, and the map is written to disk before this process serves it.  A failure at any point leaves
  the service serving exactly what it served before.

The last test is the acceptance one: a book created over HTTP takes an account and an entry immediately
afterwards, over the same client, with no restart and nothing done by hand.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from haddon.api.app import create_app
from haddon.api.config import ApiConfig, Credential, CursorKeyring, load_config

from conftest import API_ACTOR, API_TOKEN, CURSOR_SECRET, OTHER_TOKEN

NEW = "acme-holdings"


def key() -> str:
    return uuid.uuid4().hex


def creation(entity_id: str = NEW, **overrides) -> dict:
    body = {"entity_id": entity_id, "legal_name": "Acme Holdings Ltd", "legal_form": "Ltd", "country": "GB",
            "base_currency": "GBP", "base_precision": 2}
    return {**body, **overrides}


@dataclass
class Provisioning:
    """A service started from a real configuration file, which is what provisioning needs.

    Every other API fixture builds an ``ApiConfig`` in code.  That cannot work here: registering an entity
    writes the deployment's own configuration back, so a test that skipped the file would be testing a path
    production does not take.
    """

    client: object
    config: ApiConfig
    config_path: Path
    books: Path
    existing: str

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {API_TOKEN}"}

    def create(self, body: dict, *, intent: str | None = None, token: str = API_TOKEN):
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": intent or key()}
        return self.client.post("/v1/entities", headers=headers, json=body)

    def post(self, path: str, body: dict, *, intent: str | None = None):
        return self.client.post(path, headers={**self.headers, "Idempotency-Key": intent or key()}, json=body)

    def get(self, path: str):
        return self.client.get(path, headers=self.headers)

    def on_disk(self) -> dict:
        """The entity map as the file holds it: what this deployment would serve after a restart."""
        return json.loads(self.config_path.read_text(encoding="utf-8"))["entities"]


def _build(fast_dir: Path, provisioned_template: Path, *, entity_root: bool = True,
           scoped_to: tuple[str, ...] = ("*",)) -> Provisioning:
    from fastapi.testclient import TestClient

    books = fast_dir.joinpath("books")
    books.mkdir()
    existing_path = books.joinpath("existing.db")
    shutil.copy(provisioned_template, existing_path)
    con = sqlite3.connect(existing_path)
    try:
        existing = con.execute("SELECT id FROM entities").fetchone()[0]
    finally:
        con.close()
    raw = {"entities": {existing: "books/existing.db"},
           "tokens": {API_TOKEN: {"actor": API_ACTOR, "entities": list(scoped_to)},
                      OTHER_TOKEN: {"actor": "agent:elsewhere", "entities": [existing]}},
           "cursor_secret": CURSOR_SECRET}
    if entity_root:
        raw["entity_root"] = "books"
    config_path = fast_dir.joinpath("haddon.json")
    config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    config = load_config(config_path)
    return Provisioning(client=TestClient(create_app(config)), config=config, config_path=config_path,
                        books=books, existing=existing)


@pytest.fixture
def provisioning(fast_dir, provisioned_template):
    p = _build(fast_dir, provisioned_template)
    with p.client:
        yield p


# The creation itself
def test_a_created_entity_is_served_immediately_and_answers_as_the_listing_does(provisioning):
    """201, and the same object ``GET /v1/entities`` lists, so a caller can follow its links straight in."""
    created = provisioning.create(creation())
    assert created.status_code == 201, created.text[:600]
    body = created.json()
    assert body["entity_id"] == NEW
    assert (body["legal_name"], body["legal_form"], body["country"]) == ("Acme Holdings Ltd", "Ltd", "GB")
    assert body["base_currency"] == "GBP" and body["commodities"] == ["GBP"]
    assert (body["lock_through"], body["lock_version"], body["lock_close_id"]) == (None, 1, None)
    assert body["links"]["self"] == f"/v1/entities/{NEW}"

    listed = {item["entity_id"]: item for item in provisioning.get("/v1/entities").json()["items"]}
    assert set(listed) == {provisioning.existing, NEW}
    assert listed[NEW] == body

    resource = provisioning.get(f"/v1/entities/{NEW}")
    assert resource.status_code == 200 and resource.json() == body


def test_the_entity_map_is_written_to_the_configuration_file_not_just_this_process(provisioning):
    """The durability half.  An entity this process alone knows about is gone at the next restart, so the check
    is that a service rebuilt from the same file (which is what a restart is) serves it."""
    assert provisioning.create(creation()).status_code == 201
    assert provisioning.on_disk() == {provisioning.existing: "books/existing.db", NEW: f"books/{NEW}.db"}

    from fastapi.testclient import TestClient

    with TestClient(create_app(load_config(provisioning.config_path))) as restarted:
        after = restarted.get("/v1/entities", headers=provisioning.headers)
    assert [item["entity_id"] for item in after.json()["items"]] == sorted([provisioning.existing, NEW])


def test_the_configuration_keeps_everything_it_held_that_this_write_is_not_about(provisioning):
    """The file holds tokens and a cursor key.  Registering an entity re-reads and rewrites it, so nothing else
    may move.  A provisioning that quietly dropped a credential would lock the deployment out at the next
    restart."""
    before = json.loads(provisioning.config_path.read_text(encoding="utf-8"))
    assert provisioning.create(creation()).status_code == 201
    after = json.loads(provisioning.config_path.read_text(encoding="utf-8"))
    assert {k: v for k, v in after.items() if k != "entities"} == {k: v for k, v in before.items() if k != "entities"}


def test_a_new_book_is_empty_apart_from_the_account_an_opening_balance_needs(provisioning):
    """``starter_chart`` defaults to false: a book about to be authored or imported brings its own chart.

    It is not empty of everything, though.  ``initialize_entity`` writes the retained-earnings equity account
    whichever way the flag is set, because an entity that cannot take an opening balance is not a usable book.
    """
    assert provisioning.create(creation()).status_code == 201
    accounts = provisioning.get(f"/v1/entities/{NEW}/accounts").json()["items"]
    assert [a["path"] for a in accounts] == ["equity:retained-earnings"]

    assert provisioning.create(creation("acme-two", starter_chart=True)).status_code == 201
    with_chart = provisioning.get("/v1/entities/acme-two/accounts").json()["items"]
    assert "assets:cash" in {a["path"] for a in with_chart} and len(with_chart) > len(accounts)


def test_further_commodities_are_declared_at_creation(provisioning):
    body = creation(commodities=[{"code": "USD", "precision": 2}, {"code": "JPY", "precision": 0}])
    assert provisioning.create(body).status_code == 201
    assert provisioning.get(f"/v1/entities/{NEW}").json()["commodities"] == ["GBP", "JPY", "USD"]


def test_the_creation_is_on_the_audit_log_of_the_book_it_created(provisioning):
    """The record of who called this book into being lives in the book, like every other write."""
    assert provisioning.create(creation(reason="the group's UK holding company files separately")).status_code == 201
    events = provisioning.get(f"/v1/entities/{NEW}/events").json()["items"]
    initialized = [e for e in events if e["kind"] == "entity.initialized"]
    assert len(initialized) == 1
    assert initialized[0]["reason"] == "the group's UK holding company files separately"
    assert initialized[0]["created_by"] == API_ACTOR


# Refusals
@pytest.mark.parametrize("bad", ["../escape", "books/nested", "Acme", "acme_holdings", "", "-leading", "a" * 64])
def test_an_id_that_is_not_a_slug_is_refused_before_anything_is_written(provisioning, bad):
    """The id becomes a file name.  Every shape that could leave ``entity_root``, or collide on a
    case-insensitive filesystem, is refused, and the refusal names the rule rather than the symptom."""
    response = provisioning.create(creation(bad))
    assert response.status_code == 422, response.text[:400]
    assert response.json()["error"]["details"][0]["reason"] in {"entity_id_shape", "string_too_short"}
    assert sorted(p.name for p in provisioning.books.iterdir()) == ["existing.db"]
    assert provisioning.on_disk() == {provisioning.existing: "books/existing.db"}


def test_creating_an_entity_that_already_exists_is_refused_rather_than_merged(provisioning):
    """The id is the book's permanent address and the rows behind it are somebody's accounts."""
    assert provisioning.create(creation()).status_code == 201
    again = provisioning.create(creation(legal_name="A Different Company Entirely"))
    assert again.status_code == 409, again.text[:400]
    assert again.json()["error"]["code"] == "immutable"
    assert provisioning.get(f"/v1/entities/{NEW}").json()["legal_name"] == "Acme Holdings Ltd"


def test_the_same_creation_retried_replays_the_original_reply(provisioning):
    """What makes this safe to retry over a connection that dropped after the books were written."""
    intent = key()
    first = provisioning.create(creation(), intent=intent)
    assert first.status_code == 201
    retry = provisioning.create(creation(), intent=intent)
    assert retry.status_code == 201 and retry.json() == first.json()
    assert retry.headers["Idempotency-Replayed"] == "true"


def test_the_same_key_carrying_a_different_creation_is_a_conflict(provisioning):
    intent = key()
    assert provisioning.create(creation(), intent=intent).status_code == 201
    clash = provisioning.create(creation(legal_name="Something Else Ltd"), intent=intent)
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_conflict"


def test_a_creation_without_an_idempotency_key_is_refused_like_every_other_write(provisioning):
    response = provisioning.client.post("/v1/entities", headers=provisioning.headers, json=creation())
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["reason"] == "idempotency_key_required"


def test_a_credential_may_not_create_a_book_it_could_not_then_address(fast_dir, provisioned_template):
    """The one way a scoped credential could otherwise reach past its own scope."""
    p = _build(fast_dir, provisioned_template, scoped_to=("the-only-book-i-may-touch",))
    with p.client:
        response = p.create(creation())
    assert response.status_code == 403, response.text[:400]
    assert response.json()["error"]["details"][0]["reason"] == "entity_not_in_scope"
    assert sorted(x.name for x in p.books.iterdir()) == ["existing.db"]


def test_an_installation_with_nowhere_to_put_a_book_says_so(fast_dir, provisioned_template):
    """``entity_root`` unset means the deployment does not provision entities.  Same shape as ``blob_root``, and
    the right default for one whose entity map is managed by hand."""
    p = _build(fast_dir, provisioned_template, entity_root=False)
    with p.client:
        response = p.create(creation())
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["reason"] == "entity_root_unset"


def test_a_service_built_without_a_configuration_file_refuses_to_provision(fast_dir, provisioned_template):
    """An entity registered only in memory disappears at the next restart.  A book that exists until somebody
    restarts the service is worse than one that was refused, so this refuses."""
    from fastapi.testclient import TestClient

    shutil.copy(provisioned_template, fast_dir.joinpath("in-code.db"))
    config = ApiConfig(entities={"whatever-it-is": fast_dir.joinpath("in-code.db")},
                       credentials=(Credential(API_TOKEN, API_ACTOR, ("*",)),),
                       entity_root=fast_dir, cursor_keys=CursorKeyring.of(CURSOR_SECRET))
    with TestClient(create_app(config)) as client:
        response = client.post("/v1/entities", json=creation(),
                               headers={"Authorization": f"Bearer {API_TOKEN}", "Idempotency-Key": key()})
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["reason"] == "config_not_durable"


def test_a_database_file_already_holding_a_book_is_not_provisioned_over(provisioning):
    """Somebody's accounts, under the name this caller asked for, that the configuration does not mention.  It
    is for an operator to look at, not something to add a second entity's rows to."""
    shutil.copy(provisioning.books.joinpath("existing.db"), provisioning.books.joinpath(f"{NEW}.db"))
    response = provisioning.create(creation())
    assert response.status_code == 409 and response.json()["error"]["details"][0]["reason"] == "file_exists"
    assert provisioning.on_disk() == {provisioning.existing: "books/existing.db"}


def test_two_creations_of_the_same_id_at_once_produce_one_book(provisioning):
    """The race the provisioning lock exists for.

    Without it both callers see an unclaimed id, both migrate the same file, and the loser writes its entity
    row into the winner's books, or worse, registers a path to them.  With it one caller creates the book and
    the other is told it already exists, which is a refusal a caller can act on.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        both = [f.result() for f in [pool.submit(provisioning.create, creation()) for _ in range(2)]]
    assert sorted(r.status_code for r in both) == [201, 409]
    assert [item["entity_id"] for item in provisioning.get("/v1/entities").json()["items"]].count(NEW) == 1
    assert provisioning.on_disk() == {provisioning.existing: "books/existing.db", NEW: f"books/{NEW}.db"}


# The acceptance test
def test_a_book_created_over_http_takes_a_chart_and_an_entry_straight_away(provisioning):
    """End to end and unaided: an agent with a token and no shell keeps a second company.

    Create the book, add the two accounts it needs, post a balanced entry between them, and read the entry back.
    Every step goes over the same client, with no restart and nothing done by hand in between.
    """
    assert provisioning.create(creation()).status_code == 201
    base = f"/v1/entities/{NEW}"
    for path, type_, is_cash in (("assets:bank", "asset", True), ("expenses:office", "expense", False)):
        made = provisioning.post(f"{base}/accounts",
                                 {"path": path, "type": type_, "is_cash": is_cash, "commodities": ["GBP"]})
        assert made.status_code == 201, made.text[:400]

    entry = provisioning.post(f"{base}/entries", {
        "dt": "2026-03-05", "narration": "March rent", "reason": "the March rent went out on the 5th",
        "legs": [{"account": "assets:bank", "commodity": "GBP", "amount": "-100.00"},
                 {"account": "expenses:office", "commodity": "GBP", "amount": "100.00"}]})
    assert entry.status_code == 201, entry.text[:400]

    entries = provisioning.get(f"{base}/entries").json()["items"]
    assert [e["narration"] for e in entries] == ["March rent"]
    trial = provisioning.get(f"{base}/reports/trial-balance?as_of=2026-03-31").json()
    assert {(l["account_path"], l["amount_micros"]) for l in trial["lines"]} == {
        ("assets:bank", "-100000000"), ("expenses:office", "100000000")}
    assert trial["totals"] == {"GBP": "0"}
