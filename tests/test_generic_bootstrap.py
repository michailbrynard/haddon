"""A company Haddon has never heard of, from an empty directory to a closed month, using only what is published.

Issue 0006's acceptance criteria and Phase 4's exit gate, driven end to end: "an agent can maintain a normal
period using the published commands alone, including safe retry after a timeout and recovery from validation
errors."  Nothing here reads a fixture of a real company or imports a corpus: an entity is initialised from the
documented command line, served over HTTP, posted to, corrected and closed.

The other half is negative: ``src/haddon`` may name no checkout, no fixed import year and no entity list
(issue 0006, "generic modules").  The guards below fail if a deployment's checkout path, a default year or a
list of one corpus's entities appears in the runtime modules.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "src" / "haddon"


# The product names no corpus
#: A path that would put one deployment's source checkout into the product.
CHECKOUT_SHAPED = re.compile(r"corpus/example/|/opt/|/srv/|/home/")
#: A four-digit year.  1900..2100 rather than "any number", so 1000000 (a micro) and 2048 (a byte size) are not
#: mistaken for one.
YEAR_SHAPED = re.compile(r"^(19|20|21)\d\d$")
#: The entities of one corpus, by directory key and by entity id.
ENTITY_NAMED = re.compile(r"ExampleInc|ExampleBV|ExampleSL|example-inc|example-bv|example-sl")


def _python_files():
    return [p for p in sorted(PKG.rglob("*.py")) if "__pycache__" not in p.parts]


def _docstrings(tree) -> set[int]:
    """The ids of every docstring node, so prose can be told from a value the code uses."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _code_constants(path: Path):
    """Every literal the code actually uses: docstrings and comments excluded.

    Explaining in prose why the 2025/2026 continuity case is where a double count hides is documentation this
    repository wants; inheriting ``year=2025`` as a default is what issue 0006 removes.  A guard that could not
    tell them apart would be answered by deleting the explanation.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prose = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in prose:
            yield node


def test_no_module_names_a_source_checkout():
    """No module hardcodes a checkout path; the checkout is a line in a corpus manifest (``corpus/example.json``).

    The importer once had ``CHECKOUT = Path(...)`` as the default of four functions and two command lines."""
    offenders = [(path.relative_to(REPO), node.lineno, node.value)
                 for path in _python_files() for node in _code_constants(path)
                 if isinstance(node.value, str) and CHECKOUT_SHAPED.search(node.value)]
    assert offenders == []


def _inherited_numbers(path: Path):
    """Every number a caller can inherit without asking for it: a module-level constant, or an argument default.

    Not every four-digit integer, which would catch ``max_length=2048`` and teach the next person to work around
    the guard.  The target is ``BOOK_YEAR = 2025`` and ``year: int = BOOK_YEAR`` (issue 0006): a year a caller
    gets by saying nothing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: list[ast.AST] = []
    for node in tree.body:  # module level only: a local `year = 2025` inside a test helper is not a default
        if isinstance(node, ast.Assign):
            targets.append(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets.append(node.value)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            targets.extend([d for d in node.args.defaults if d is not None])
            targets.extend([d for d in node.args.kw_defaults if d is not None])
    for target in targets:
        for node in ast.walk(target):
            if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                    and not isinstance(node.value, bool):
                yield node


def test_no_module_carries_a_fixed_book_year():
    """No default year such as ``BOOK_YEAR = 2025`` or ``FROM_YEAR, TO_YEAR = 2025, 2026``.

    Which years a set of journals covers is a fact about those journals, not about a ledger."""
    offenders = [(path.relative_to(REPO), node.lineno, node.value)
                 for path in _python_files() for node in _inherited_numbers(path)
                 if YEAR_SHAPED.match(str(node.value))]
    assert offenders == []


def test_no_module_holds_a_list_of_entities():
    """No module names a corpus's entities (as ``ENTITIES`` or ``DEFAULT_ENTITIES`` once did); they belong in
    ``corpus/``."""
    offenders = [(path.relative_to(REPO), node.lineno)
                 for path in _python_files() for node in _code_constants(path)
                 if isinstance(node.value, str) and ENTITY_NAMED.search(node.value)]
    assert offenders == []


def test_no_data_file_inside_the_package_belongs_to_one_corpus():
    """No data file in the package holds one corpus's figures.  A manifest's data belongs with the manifest, in
    ``corpus/``."""
    offenders = [p.relative_to(REPO) for p in sorted(PKG.rglob("*"))
                 if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts
                 and ENTITY_NAMED.search(p.read_text(encoding="utf-8", errors="ignore"))]
    assert offenders == []


def test_a_corpus_is_read_from_a_manifest_and_refused_when_unnamed(monkeypatch, fast_dir):
    """The loader raises rather than guessing.  A ledger that guessed which company's books it was verifying
    against would be worse than one that says which variable to set."""
    from haddon.domain.errors import HaddonError
    from haddon.importing import corpus as corpus_module

    corpus = corpus_module.read_corpus(REPO / "corpus" / "example.json")
    # A relative path in a manifest is relative to the manifest, never to the working directory.
    assert corpus.checkout == REPO / "corpus" / "example"
    assert corpus.read_only_roots == (REPO / "corpus" / "example",)
    assert corpus.default_year == 2025 and corpus.continuity_years == (2025, 2026)
    assert set(corpus.entity_keys) == {"ExampleInc", "ExampleBV", "ExampleSL"}
    assert corpus.journal("ExampleBV").name == "2025.journal"
    assert corpus.journal("ExampleBV", 2026).name == "2026.journal"
    # How far back a company's books go is that company's fact, not the corpus's: the S.L. has a 2024 journal,
    # while the B.V. and the Inc. have no 2024 ledger.  A single corpus-wide chain would be wrong for one of the
    # three.
    assert corpus.continuity_years_for("ExampleSL") == (2024, 2025, 2026)
    assert corpus.continuity_years_for("ExampleBV") == (2025, 2026)
    assert corpus.continuity_years_for("ExampleInc") == (2025, 2026)

    monkeypatch.delenv(corpus_module.CORPUS_ENV, raising=False)
    with pytest.raises(HaddonError) as raised:
        corpus_module.load_corpus()
    assert corpus_module.CORPUS_ENV in str(raised.value)


# What a manifest says when it is wrong (0006 review)
GOOD_MANIFEST = {
    "name": "one-company", "checkout": "/tmp/books", "default_year": 2025, "continuity_years": [2024, 2025],
    "entities": [{"key": "Acme", "id": "acme", "name": "Acme Ltd", "legal_form": "Ltd",
                  "country": "US", "base_currency": "USD"}],
}


def _manifest(tmp_path: Path, body, *, name: str = "m.json") -> Path:
    path = tmp_path / name
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return path


def _second_entity(**over) -> dict:
    return {"key": "Acme", "id": "acme-2", "name": "Acme Holdings", "legal_form": "BV",
            "country": "NL", "base_currency": "EUR", **over}


def test_a_manifest_declaring_one_key_twice_is_refused_by_name(fast_dir):
    """A duplicated key is refused, not resolved to whichever block came last (0006 review).

    Otherwise an operator who copied a block while adding a company would import one entity's journal under
    another's id and legal name, and nothing would say so.
    """
    from haddon.domain.errors import HaddonError
    from haddon.importing import corpus as corpus_module

    body = {**GOOD_MANIFEST, "entities": [GOOD_MANIFEST["entities"][0], _second_entity()]}
    with pytest.raises(HaddonError) as raised:
        corpus_module.read_corpus(_manifest(fast_dir, body, name="dup.json"))
    message = str(raised.value)
    # The key, both ids, and the file: enough to find the block without opening anything else.
    assert "'Acme'" in message and "acme" in message and "acme-2" in message and "dup.json" in message
    assert raised.value.details[0]["reason"] == "duplicate_key"


@pytest.mark.parametrize("label,body,expect", [
    ("malformed json", '{"name": "broken",', "malformed_json"),
    ("not an object", '["a list"]', "object_required"),
    ("entity is a string", {**GOOD_MANIFEST, "entities": ["Acme"]}, "object_required"),
    ("entities is not a list", {**GOOD_MANIFEST, "entities": {"key": "Acme"}}, "list_required"),
    ("year is not an integer", {**GOOD_MANIFEST, "default_year": "two thousand"}, "year_required"),
    ("continuity year is a string", {**GOOD_MANIFEST, "continuity_years": ["2024", 2025]}, "year_required"),
    ("continuity years is one year", {**GOOD_MANIFEST, "continuity_years": [2025]}, "shape"),
    ("continuity years run backwards", {**GOOD_MANIFEST, "continuity_years": [2026, 2025]}, "order"),
    ("continuity years repeat", {**GOOD_MANIFEST, "continuity_years": [2025, 2025]}, "order"),
    ("entity continuity years run backwards",
     {**GOOD_MANIFEST, "entities": [{**GOOD_MANIFEST["entities"][0], "continuity_years": [2026, 2024]}]},
     "order"),
    ("entity continuity year is a string",
     {**GOOD_MANIFEST, "entities": [{**GOOD_MANIFEST["entities"][0], "continuity_years": [2024, "2025"]}]},
     "year_required"),
    ("entity field missing", {**GOOD_MANIFEST,
                              "entities": [{k: v for k, v in GOOD_MANIFEST["entities"][0].items()
                                            if k != "base_currency"}]}, "required"),
    ("entity field unknown", {**GOOD_MANIFEST,
                              "entities": [{**GOOD_MANIFEST["entities"][0], "currency": "USD"}]},
     "unknown_field"),
    ("entity field is not a string", {**GOOD_MANIFEST,
                                      "entities": [{**GOOD_MANIFEST["entities"][0], "id": 7}]},
     "string_required"),
    ("checkout is missing", {k: v for k, v in GOOD_MANIFEST.items() if k != "checkout"}, "required"),
])
def test_a_broken_manifest_names_the_file_and_the_field(fast_dir, label, body, expect):
    """``HADDON_CORPUS`` is the single source with no fallback (issue 0006), so a refusal must say what to fix.

    Each case must name the file and the field, not escape as a raw ``JSONDecodeError``, ``TypeError`` or
    ``invalid literal for int()``.
    """
    from haddon.domain.errors import HaddonError
    from haddon.importing import corpus as corpus_module

    path = _manifest(fast_dir, body, name=f"{label.replace(' ', '-')}.json")
    with pytest.raises(HaddonError) as raised:
        corpus_module.read_corpus(path)
    assert raised.value.code == "validation", label
    assert path.name in str(raised.value), (label, str(raised.value))
    assert raised.value.details[0]["reason"] == expect, (label, raised.value.details)
    assert raised.value.details[0]["path"], label


def test_the_good_manifest_these_are_mutations_of_still_reads(fast_dir):
    """The positive control: every case above differs from this by one field."""
    from haddon.importing import corpus as corpus_module

    corpus = corpus_module.read_corpus(_manifest(fast_dir, GOOD_MANIFEST, name="good.json"))
    assert corpus.entity_keys == ("Acme",)
    assert corpus.default_year == 2025 and corpus.continuity_years == (2024, 2025)
    assert corpus.baselines is None and corpus.read_only_roots == ()
    # An entity that declares nothing inherits the corpus's chain; absent is not the same as empty.
    assert corpus.entity("Acme").continuity_years is None
    assert corpus.continuity_years_for("Acme") == (2024, 2025)


def test_an_entity_may_declare_the_years_its_own_books_run_over(fast_dir):
    """A corpus holds companies of different ages.  The chain an entity declares is the chain a continuity run
    loads for it, and it overrides the corpus's without changing it for anybody else."""
    from haddon.importing import corpus as corpus_module

    body = {**GOOD_MANIFEST, "entities": [GOOD_MANIFEST["entities"][0],
                                          _second_entity(key="Older", id="older",
                                                         continuity_years=[2023, 2024, 2025])]}
    corpus = corpus_module.read_corpus(_manifest(fast_dir, body, name="ages.json"))
    assert corpus.continuity_years_for("Acme") == (2024, 2025)
    assert corpus.continuity_years_for("Older") == (2023, 2024, 2025)
    assert corpus.continuity_years == (2024, 2025)      # the corpus's own is untouched


def test_the_wrappers_do_not_reinstate_the_fallback_the_loader_refuses(fast_dir):
    """``bin/haddon-import`` and ``bin/haddon-oracle`` must not default ``HADDON_CORPUS`` (0006 review).

    A ``${HADDON_CORPUS:-...}`` default in a wrapper would undo the loader's refusal to guess in the command
    people actually type.  ``bin/haddon-export`` keeps its default on purpose: there the corpus is an input to
    the read-only guard, never a choice of whose books to read.
    """
    for name in ("haddon-import", "haddon-oracle"):
        text = (REPO / "bin" / name).read_text()
        assert "HADDON_CORPUS:-" not in text, f"bin/{name} defaults HADDON_CORPUS; the loader refuses to"
    assert "HADDON_CORPUS:-" in (REPO / "bin" / "haddon-export").read_text()
    # The repository names its own manifest in the one place that is not a wrapper.
    assert "export HADDON_CORPUS ?=" in (REPO / "Makefile").read_text()


# A new entity, end to end
def haddon_init(db: Path, **over) -> dict:
    """``bin/haddon-init``, run the way ``docs/quickstart.md`` documents it."""
    args = {"--id": "fernside-audio", "--name": "Fernside Audio GmbH", "--legal-form": "GmbH",
            "--country": "DE", "--base-currency": "EUR", "--base-precision": "2", **over}
    flat = [item for pair in args.items() for item in pair]
    proc = subprocess.run([sys.executable, "-m", "haddon.initialize", str(db), *flat],
                          capture_output=True, text=True, cwd=REPO)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture
def new_entity(fast_dir):
    """A company this repository has no fixture for, initialised and served."""
    from conftest import build_api

    db = fast_dir / "fernside-audio.db"
    initialized = haddon_init(db)
    config, client = build_api({"fernside-audio": db})
    return initialized, config, client


class Agent:
    """The published surface, as an agent holds it: a token, a base path and one intent key per intent."""

    def __init__(self, client, entity_id: str):
        from conftest import API_TOKEN

        self.client, self.base = client, f"/v1/entities/{entity_id}"
        self.headers = {"Authorization": f"Bearer {API_TOKEN}"}

    def get(self, path: str):
        return self.client.get(f"{self.base}{path}", headers=self.headers)

    def post(self, path: str, body: dict, key: str):
        return self.client.post(f"{self.base}{path}", json=body,
                                headers={**self.headers, "Idempotency-Key": key})


def test_a_new_entity_is_initialized_from_one_documented_command(new_entity):
    initialized, _, client = new_entity
    assert initialized["entity_id"] == "fernside-audio"
    assert initialized["base_currency"] == "EUR"
    assert [c["code"] for c in initialized["commodities"]] == ["EUR"]
    # The starter chart plus the equity account an opening balance lands in.
    assert "equity:retained-earnings" in {a["path"] for a in initialized["accounts"]}
    assert initialized["audit"]["kind"] == "entity.initialized"
    with client:
        agent = Agent(client, "fernside-audio")
        assert agent.get("/capabilities").status_code == 200


def test_an_agent_maintains_a_period_with_the_published_commands_alone(new_entity):
    """Phase 4's exit gate, in the order an agent would actually work: extend the chart, accrue, restate what
    was wrong, load the month's bank statement, explain its line, and close.

    Every call uses an account path and a decimal amount.  The only ids the agent sends back are ones a previous
    command handed it, and it invents none: no snapshot, no decision, no explanation id.
    """
    _, _, client = new_entity
    with client:
        agent = Agent(client, "fernside-audio")
        published = agent.get("/capabilities").json()
        commands = {c["name"] for c in published["commands"]}
        assert {"create_account", "post_entry", "correct_entry", "load_statement", "book_line",
                "close_period"} <= commands

        # 1. the chart has no home for what this company spends money on, so the agent writes one
        created = agent.post("/accounts", {"path": "expenses:studio-rent", "type": "expense",
                                           "commodities": ["EUR"],
                                           "reason": "the studio lease starts in March"}, key="acct-1")
        assert created.status_code == 201, created.text

        # 2. an accrual: an economic event on a date with no bank row behind it
        accrued = agent.post("/entries", {
            "dt": "2026-03-01", "narration": "Insurance, March", "reason": "the policy renewed on the 1st",
            "legs": [{"account": "expenses:general", "commodity": "EUR", "amount": "320.50"},
                     {"account": "liabilities:payables", "commodity": "EUR", "amount": "-320.50"}]}, key="entry-1")
        assert accrued.status_code == 201, accrued.text
        wrong = accrued.json()["entry"]["id"]

        # 3. it was the wrong amount: restate it, do not merely withdraw it
        corrected = agent.post(f"/entries/{wrong}/correct", {
            "reason": "the renewal notice says 340.50, and the invoice agrees",
            "replacement": {"dt": "2026-03-01", "narration": "Insurance, March",
                            "legs": [{"account": "expenses:general", "commodity": "EUR", "amount": "340.50"},
                                     {"account": "liabilities:payables", "commodity": "EUR",
                                      "amount": "-340.50"}]}}, key="correct-1")
        assert corrected.status_code == 201, corrected.text
        body = corrected.json()
        assert agent.get(f"/entries/{body['mirror_id']}").json()["entry"]["status"] == "posted"
        assert agent.get(f"/entries/{body['replacement_id']}").json()["entry"]["status"] == "posted"

        # 4. the month's bank statement, in the same vocabulary: a path and decimal balances
        loaded = agent.post("/statements", {
            "account": "assets:cash", "commodity": "EUR",
            "period_start": "2026-03-01", "period_end": "2026-03-31",
            "opening": "0", "closing": "-1450.00",
            "lines": [{"dt": "2026-03-02", "description": "STUDIO LEASE MAR", "amount": "-1450.00",
                       "currency": "EUR", "reference": "TX-3301"}],
            "document": {"sha256": "a" * 64, "filename": "march.csv", "media_type": "text/csv",
                         "size_bytes": 512, "blob_path": "/var/haddon/blobs/march.csv"},
            "locator": "page 1, closing balance", "reason": "the March statement, as downloaded"}, key="stmt-1")
        assert loaded.status_code == 201, loaded.text
        statement_id = loaded.json()["statement"]["statement_id"]
        line = agent.get(f"/statements/{statement_id}/lines").json()["items"][0]

        # 5. and what that line is: the entry it stands for, posted and held against it in one call
        booked = agent.post(f"/lines/{line['line_id']}/book", {
            "narration": "Studio rent, March", "reason": "the lease invoice for March",
            "legs": [{"account": "expenses:studio-rent", "commodity": "EUR", "amount": "1450.00"}]}, key="book-1")
        assert booked.status_code == 201, booked.text
        assert booked.json()["line"]["status"] == "booked"

        # 6. the books now read as the month happened: the restated accrual, and the rent the bank paid
        balances = {line["account_path"]: line["amount_micros"]
                    for line in agent.get("/reports/trial-balance?as_of=2026-03-31").json()["lines"]}
        assert balances["expenses:studio-rent"] == "1450000000"
        assert balances["expenses:general"] == "340500000"

        # 7. what would a close refuse?  Nothing, so close, with the lock version just read.
        checks = agent.get("/close-checks?through=2026-03-31").json()
        assert checks["eligible"] is True, checks["checks"]
        closed = agent.post("/close", {"through": "2026-03-31", "reason": "March is reconciled and reviewed",
                                       "expected_version": checks["lock"]["version"], "overrides": []},
                            key="close-1")
        assert closed.status_code == 201, closed.text
        assert closed.json()["lock"]["through"] == "2026-03-31"

        # 8. and the closed month is shut: a further entry in it is refused
        late = agent.post("/entries", {
            "dt": "2026-03-20", "narration": "A late invoice", "reason": "it arrived after the close",
            "legs": [{"account": "expenses:general", "commodity": "EUR", "amount": "10.00"},
                     {"account": "liabilities:payables", "commodity": "EUR", "amount": "-10.00"}]}, key="late-1")
        assert late.status_code == 409 and late.json()["error"]["code"] == "period_locked"


def test_a_retry_after_a_timeout_replays_rather_than_posting_twice(new_entity):
    """Safe retry, which the exit gate names: an agent that never sees the reply must be able to send it again."""
    _, _, client = new_entity
    with client:
        agent = Agent(client, "fernside-audio")
        body = {"dt": "2026-04-02", "narration": "Invoice 0041", "reason": "the April invoice was raised",
                "legs": [{"account": "assets:receivables", "commodity": "EUR", "amount": "7316.67"},
                         {"account": "income:sales", "commodity": "EUR", "amount": "-7316.67"}]}
        key = str(uuid.uuid4())
        first = agent.post("/entries", body, key=key)
        again = agent.post("/entries", body, key=key)
        assert first.status_code == again.status_code == 201
        assert again.headers["Idempotency-Replayed"] == "true"
        assert first.json()["entry"]["id"] == again.json()["entry"]["id"]
        assert len(agent.get("/entries").json()["items"]) == 1

        # The same key carrying a different intent is a conflict, not a second silent replay.
        conflict = agent.post("/entries", {**body, "narration": "Invoice 0042"}, key=key)
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize("body,path,says", [
    # ``path`` is what the refusal must name (the exact field, or for a whole-entry rule the commodity that
    # does not balance), and ``says`` is the invariant it broke.
    ({"legs": [{"account": "expenses:nowhere", "commodity": "EUR", "amount": "10.00"},
               {"account": "assets:cash", "commodity": "EUR", "amount": "-10.00"}]},
     "legs[0].account", "account_unknown"),
    ({"legs": [{"account": "expenses:general", "commodity": "CHF", "amount": "10.00"},
               {"account": "assets:cash", "commodity": "EUR", "amount": "-10.00"}]},
     "legs[0].commodity", "commodity_unknown"),
    ({"legs": [{"account": "expenses:general", "commodity": "EUR", "amount": "10.005"},
               {"account": "assets:cash", "commodity": "EUR", "amount": "-10.005"}]},
     "legs[0].amount", "precision"),
    ({"legs": [{"account": "expenses:general", "commodity": "EUR", "amount": "10.00"},
               {"account": "assets:cash", "commodity": "EUR", "amount": "-9.00"}]},
     "EUR", "unbalanced"),
])
def test_a_validation_error_says_which_field_and_what_rule(new_entity, body, path, says):
    """The other half of the exit gate.  An agent recovers from a refusal only if the refusal says what to
    change: the exact field, and the invariant it broke, not "invalid request"."""
    _, _, client = new_entity
    with client:
        agent = Agent(client, "fernside-audio")
        response = agent.post("/entries", {
            "dt": "2026-05-04", "narration": "A booking that will not stand",
            "reason": "checking what a refusal tells an agent", **body}, key=str(uuid.uuid4()))
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        detail = json.dumps(error["details"])
        assert path in detail, (path, detail)
        assert says in detail or says in error["code"], (says, error)
        # Nothing was written, so the same key is free for the corrected attempt.
        assert agent.get("/entries").json()["items"] == []


def test_an_unknown_account_path_names_the_account_it_could_not_find(new_entity):
    """"Useful candidates where safe" (issue 0006): the refusal repeats what was asked for, so an agent can tell
    a typo from an account it genuinely has to create first."""
    _, _, client = new_entity
    with client:
        agent = Agent(client, "fernside-audio")
        response = agent.post("/entries", {
            "dt": "2026-05-04", "narration": "n", "reason": "r",
            "legs": [{"account": "expences:general", "commodity": "EUR", "amount": "10.00"},
                     {"account": "assets:cash", "commodity": "EUR", "amount": "-10.00"}]},
            key=str(uuid.uuid4()))
        assert response.json()["error"]["details"][0]["account"] == "expences:general"
