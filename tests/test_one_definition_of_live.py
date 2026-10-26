"""One definition of "live", enforced structurally.

The reconciliation extension derives "live" in SQL: a statement nothing replaces (``live_statements``), and an
explanation nothing supersedes whose statement is itself live (``live_explanations``).  The second half is easy
to forget and expensive to get wrong: replacing a statement retires its lines' explanations with it, so their
allocations stop consuming their postings.

Issue 0004's review found Python re-deriving this in five places, three of which checked only ``supersedes_id``
and so counted allocations the database had already retired (a close check reported a posting as partly
allocated when it was not).  The derivation is now written once: ``tables.live_statement_clause`` /
``tables.live_explanation_clause``, plus ``statements.successor_of`` for callers that must name the replacement.
This file stops another copy appearing.  A hand-rolled ``replaces_id ==`` or ``supersedes_id ==`` over
``statements`` or ``explanations`` outside the definition sites is a failure, not a style preference.

The other supersession chains (``document_expectations``, ``unrecovered_tax``, and the ``explanations`` chain
walked in Python once it has been fetched) are not this rule.  They are different facts, and each has exactly
one reader.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "haddon"

#: where "live" is allowed to be spelled out.  Everything else asks one of these.
DEFINITION_SITES = {
    "repository/tables.py",            # live_statement_clause / live_explanation_clause: the two clauses
    "repository/statements.py",        # successor_of: the same fact, for one statement, by name
}

#: the columns that mean "something replaced this" for the reconciliation records
LIVE_COLUMNS = {"replaces_id", "supersedes_id"}

#: the tables those columns mean it *for*.  A ``supersedes_id`` on ``document_expectations`` is a different
#: chain with its own single reader and is not what this rule is about.
LIVE_TABLES = {"statements", "explanations"}


def _sources() -> list[tuple[str, Path]]:
    return [(str(p.relative_to(SRC)).replace("\\", "/"), p)
            for p in sorted(SRC.rglob("*.py")) if "__pycache__" not in p.parts]


def _base(node) -> tuple[str | None, list[str]]:
    """``t.statements.alias("x").c.replaces_id`` -> ``("t", ["statements", "alias", "c", "replaces_id"])``."""
    parts: list[str] = []
    while True:
        if isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            break
    return (node.id if isinstance(node, ast.Name) else None), list(reversed(parts))


def _aliases(tree: ast.AST) -> dict[str, str]:
    """``successor = t.statements.alias("successor")`` -> ``{"successor": "statements"}``.

    Resolving aliases matters: the readers that drifted all spelled it ``successor.c.replaces_id ==
    t.statements.c.id``, which a check matching only the literal table name would miss.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not isinstance(func, ast.Attribute) or func.attr != "alias":
            continue
        _, parts = _base(func)
        table = next((p for p in parts if p in LIVE_TABLES), None)
        if table is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = table
    return out


def _hand_rolled(path: Path) -> list[tuple[int, str]]:
    """Comparisons that re-derive "live" over ``statements`` or ``explanations``, through any ordinary spelling.

    ``replaces_id`` exists on ``statements`` alone, so any comparison of it is about statement liveness whatever
    it is reached through.  ``supersedes_id`` is shared with two unrelated chains, so it is only a finding when
    the column hangs off ``explanations``, directly or through an alias.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    aliases = _aliases(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Attribute):
            continue
        column = node.left.attr
        if column not in LIVE_COLUMNS:
            continue
        name, parts = _base(node.left)
        table = next((p for p in parts if p in LIVE_TABLES), None) or aliases.get(name or "")
        if column == "replaces_id" or table in LIVE_TABLES:
            found.append((node.lineno, column))
    return found


def test_only_the_definition_sites_derive_live_by_hand():
    offenders = []
    for rel, path in _sources():
        if rel in DEFINITION_SITES:
            continue
        for line, column in _hand_rolled(path):
            offenders.append(f"{rel}:{line} re-derives 'live' from {column}")
    assert not offenders, (
        "'live' has one definition (tables.live_statement_clause / live_explanation_clause / "
        "statements.successor_of) and these do not use it:\n" + "\n".join(offenders))


def test_the_check_is_not_vacuous():
    """The definition sites really do contain what the rule exempts them for, so this test fails loudly if the
    helpers are renamed away rather than silently passing over a package that no longer derives anything."""
    for rel in DEFINITION_SITES:
        assert _hand_rolled(SRC / rel), f"{rel} no longer derives 'live'; move or delete its exemption"


def test_the_python_clause_and_the_sql_view_agree(seeded_engine):
    """Not just one definition in Python: the same definition the database uses.

    A replaced statement's explanations are retired by the view, and the clause must retire exactly the same
    rows.  Compared as sets of ids so a disagreement names which rows, rather than a count that hides which.
    """
    import sqlalchemy as sa

    from haddon.repository import tables as t

    with seeded_engine.connect() as conn:
        if True:
            entity = conn.execute(sa.text("SELECT id FROM entities")).scalar_one()
            from_view = {r[0] for r in conn.execute(sa.text("SELECT id FROM live_explanations"))}
            from_clause = {r[0] for r in conn.execute(
                sa.select(t.explanations.c.id).where(t.explanations.c.entity_id == entity,
                                                     t.live_explanation_clause(entity)))}
            assert from_view, "the fixture must hold live explanations, or this compares two empty sets"
            assert from_view == from_clause

            view_statements = {r[0] for r in conn.execute(sa.text("SELECT id FROM live_statements"))}
            clause_statements = {r[0] for r in conn.execute(
                sa.select(t.statements.c.id).where(t.statements.c.entity_id == entity,
                                                   t.live_statement_clause()))}
            assert view_statements and view_statements == clause_statements
