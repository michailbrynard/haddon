"""The core/extension dependency rule (Phase 0, issue 0001).

Extensions may import the journal core; the core may not import an extension.  The rule is expressed twice:

1. Imports.  Every module under ``src/haddon`` is classified into an area.  A core module that imports a
   reconciliation, review, artifact or local-tax module fails.  Only core modules are checked as importers; the
   transport (``api``/``ui``) is the composition root.

2. Table coupling.  A core repository module that names an extension table (``t.statements``,
   ``t.explanations``, ...) is a dependency the import graph cannot see.  A new one must be classified, not
   silently added.  The usual fix is a hook the extension registers, as ``entries.register_allocation_guard``
   (issue 0004) and ``repository/close_coverage.py`` (issue 0005) do.

The test fails when a new module is not classified, so the model cannot drift by omission.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
PKG = SRC / "haddon"

CORE = "core"
RECONCILIATION = "reconciliation"
REVIEW = "review"
ARTIFACT = "artifact"
LOCAL_TAX = "local_tax"
TRANSITIONAL = "transitional"
TRANSPORT = "transport"

#: The journal core may not import any of these.
EXTENSION_AREAS = frozenset({RECONCILIATION, REVIEW, ARTIFACT, LOCAL_TAX})

#: One area per package; anything more specific is named in ``_REPOSITORY_AREAS``.
_AREA_BY_PACKAGE = {
    "haddon": CORE,
    "haddon.db": CORE,
    "haddon.domain": CORE,
    "haddon.reports": CORE,
    "haddon.repository": CORE,
    "haddon.api": TRANSPORT,
    "haddon.ui": TRANSPORT,
    # A backup is a file made out of the journal and checked against it, the same family as an export:
    # `haddon.backup` compares a restored database by the `state_hash` of the journal it renders, so it imports
    # the exporter.  Classifying it here also means nothing in the core may import it.
    "haddon.backup": ARTIFACT,
    "haddon.exporting": ARTIFACT,
    "haddon.importing": ARTIFACT,
    "haddon.oracle": ARTIFACT,
    "haddon.statements": ARTIFACT,
}

#: The top-level modules of ``haddon`` itself.  The package defaults to ``core``, so an extension's command module
#: beside ``commands.py`` has to be named here, which is what makes the import rule check it.
#: ``reconciliation_commands`` is the extension's typed surface (issue 0006): it may import the core, and
#: ``commands.py`` may not import it.
_TOP_LEVEL_AREAS = {
    "reconciliation_commands": RECONCILIATION,
    # The inventory counts the routes the app mounted, so it imports the composition root.  On the package
    # default it would be a core module importing ``haddon.api``, the dependency rule backwards.  Like the
    # transport, it sits above every area, and nothing may import it.
    "inventory": TRANSPORT,
}

#: The repository is one package holding every area; issue 0001 does not require splitting it.
_REPOSITORY_AREAS = {
    "tables": CORE,
    "ctx": CORE,
    "errors": CORE,
    "transaction": CORE,
    "events": CORE,
    "requests": CORE,
    "provisioning": CORE,
    "journal": CORE,
    "entries": CORE,
    "corrections": CORE,
    "closing": CORE,
    "close_checks": CORE,
    "close_coverage": RECONCILIATION,
    "audit": CORE,
    "statements": RECONCILIATION,
    "lines": RECONCILIATION,
    "reconcile": RECONCILIATION,
    "documents": REVIEW,
    "expectations": REVIEW,
    "exports": ARTIFACT,
    "unrecovered": LOCAL_TAX,
}

#: A core module that names one of these tables is coupled to an extension.  Pinned, not allowed to grow.
_EXTENSION_TABLES = (
    "documents", "statements", "statement_lines", "explanations", "allocations",
    "evidence_links", "document_expectations", "unrecovered_tax", "journal_mappings", "exports",
)
#: Each entry is a known, temporary coupling and names the issue that removes or splits it.  The import test
#: cannot see a SQL reference, so this list is the only place a new one can appear, and a new one must be a
#: deliberate decision with an issue.
_TABLE_COUPLING_ALLOWLIST = {
    # Splitting the provenance half out changes the published ``/reports/composition`` contract and the page
    # that reads it, which is the reviewer surface issue 0007 owns.  Issue 0006 left it in place.
    "reports/composition.py": "0007/0008: the report counts import provenance, which belongs with the importer",
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _area_of(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "haddon" and parts[1] == "repository" and len(parts) == 3:
        return _REPOSITORY_AREAS.get(parts[2])
    if len(parts) == 2 and parts[0] == "haddon" and parts[1] in _TOP_LEVEL_AREAS:
        return _TOP_LEVEL_AREAS[parts[1]]
    for prefix in sorted(_AREA_BY_PACKAGE, key=len, reverse=True):
        if module == prefix or module.startswith(prefix + "."):
            return _AREA_BY_PACKAGE[prefix]
    return None


def _modules() -> list[tuple[Path, str, str]]:
    out = []
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        name = _module_name(path)
        area = _area_of(name)
        if area is not None:
            out.append((path, name, area))
    return out


def _imports(path: Path) -> set[str]:
    """Every dotted haddon module imported, including members imported from a package.

    ``from haddon.repository import statements as sm`` is an import of the statements module, not merely of its
    core parent package.  Resolving known member modules here prevents that common spelling from bypassing the
    boundary.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    modules = {name for _, name, _ in _modules()}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("haddon"))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("haddon"):
            found.add(node.module)
            found.update(candidate for alias in node.names
                         if (candidate := f"{node.module}.{alias.name}") in modules)
    return found


def _extension_table_refs(path: Path) -> list[tuple[int, str]]:
    """Extension table names reached through any ordinary alias/import spelling."""
    tree = ast.parse(path.read_text(), filename=str(path))
    table_modules: set[str] = set()
    direct: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "haddon.repository.tables" and alias.asname:
                    table_modules.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "haddon.repository":
            for alias in node.names:
                if alias.name == "tables":
                    table_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "haddon.repository.tables":
            for alias in node.names:
                if alias.name in _EXTENSION_TABLES:
                    direct[alias.asname or alias.name] = alias.name
    refs = [(node.lineno, node.attr) for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in table_modules and node.attr in _EXTENSION_TABLES]
    refs.extend((node.lineno, direct[node.id]) for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id in direct)
    return sorted(set(refs))


def test_every_module_is_classified():
    modules = _modules()
    assert len(modules) >= 90  # the walk saw the package, not an empty glob
    # Every file must resolve: _modules drops nothing silently, because _area_of is total over the package tree.
    all_py = [p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts]
    assert len(modules) == len(all_py), sorted(_module_name(p) for p in all_py if _area_of(_module_name(p)) is None)


def test_the_journal_core_does_not_import_an_extension():
    violations: list[str] = []
    for path, name, area in _modules():
        if area != CORE:
            continue
        for imported in sorted(_imports(path)):
            imported_area = _area_of(imported)
            if imported_area in EXTENSION_AREAS:
                violations.append(f"{name} (core) imports {imported} ({imported_area})")
    assert not violations, "core/extension boundary crossed:\n" + "\n".join(violations)


def test_the_core_does_not_reach_into_extension_tables():
    violations: list[str] = []
    for path, name, area in _modules():
        if area != CORE:
            continue
        rel = str(path.relative_to(PKG)).replace("\\", "/")
        if rel in _TABLE_COUPLING_ALLOWLIST:
            continue
        for line, table in _extension_table_refs(path):
            violations.append(f"{rel}:{line} references extension table {table}")
    assert not violations, (
        "core module references an extension table; classify the module or extend the allowlist with a reason:\n"
        + "\n".join(violations)
    )


def test_the_allowlist_is_checked_and_each_entry_is_really_coupled():
    """The exception list is a claim about today's code, so every entry is checked: the file exists and really
    does name at least one extension table.  A stale allowance must not outlive the coupling it excuses."""
    assert _TABLE_COUPLING_ALLOWLIST, "the allowlist shrank to nothing; delete this test with it"
    for rel, reason in _TABLE_COUPLING_ALLOWLIST.items():
        assert reason, rel
        path = PKG / rel
        assert path.is_file(), rel
        assert _extension_table_refs(path), f"{rel} no longer names an extension table; remove its allowance"


def test_from_package_import_resolves_the_imported_module(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("from haddon.repository import statements as sm\n")
    assert "haddon.repository.statements" in _imports(probe)


def test_extension_table_scan_is_alias_aware(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text("from haddon.repository import tables as ledger_tables\nvalue = ledger_tables.allocations\n")
    assert _extension_table_refs(probe) == [(2, "allocations")]


def test_capabilities_does_not_pretend_to_label_extensions_yet():
    """Capabilities does not claim a core/extension label before one exists (issues 0006/0008 own it).

    A half-built ``area``/``extension`` field would mislead a reader into thinking the surface is classified.
    """
    from haddon.api import schemas

    fields = schemas.Capabilities.model_fields
    assert not {"area", "extension", "extensions"}.intersection(fields), (
        "capabilities grew an area/extension field; issue 0006 owns the real, permission-aware labelling"
    )
