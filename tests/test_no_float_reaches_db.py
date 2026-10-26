"""The float gate.  Integer micro-units are the contract; a float anywhere on the amount path is a correctness
defect, not a style one.  Five layers:

a. a float cannot get in through the repository API (Leg / Micros / Micros.parse / Micros.from_decimal);
b. a float cannot survive in the database even if it somehow got there (STRICT INTEGER columns);
c. a whole-database sweep after a realistic sequence: no column of any table holds a REAL;
d. the source tree is float-free on the amount path (AST scan of src/haddon with a justified allowlist);
e. the decimal boundary is exact (0.1 + 0.2 == 0.3, 7 places refused, quantum refused, round trips at the extremes).
"""

from __future__ import annotations

import ast
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from haddon.domain.errors import HaddonError
from haddon.domain.micros import INT64_MAX, MAX_MICROS, MIN_MICROS, Commodity, Micros, sum_micros
from haddon import commands
from haddon.repository import corrections, entries, events, requests
from haddon.repository.entries import Leg

from repo_helpers import AGENT, HUMAN, lock_books, m, pair

USD = Commodity("USD", 2)
XAU = Commodity("XAU", 6)
JPY = Commodity("JPY", 0)


def err(ei) -> HaddonError:
    assert isinstance(ei.value, HaddonError)
    return ei.value


# a. the API refuses floats
@pytest.mark.parametrize("amount,reason", [(12.5, "string_required"), (12, "string_required"), (True, "string_required"),
                                           (12.0, "string_required"), (Decimal("12.5"), "string_required")])
def test_leg_with_non_string_amount_is_422_validation(fx, amount, reason):
    legs = [Leg(fx.acct("expenses:office"), "USD", amount), Leg(fx.acct("assets:cash"), "USD", "-10000")]
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            entries.create_draft(ctx, dt="2026-03-15", narration="n", origin="manual", legs=legs)
        e = err(ei)
        assert (e.code, e.status) == ("validation", 422)
        assert e.details[0]["path"] == "legs[0].amount" and e.details[0]["reason"] == reason
        # positive control: the same leg with the wire form is accepted
        d = entries.create_draft(ctx, dt="2026-03-15", narration="n", origin="manual", legs=pair(fx, "10000"))
        assert d.legs[0].amount == m("10000")
    assert fx.scalar("SELECT count(*) FROM postings WHERE typeof(amount_micros) <> 'integer'") == 0


def test_micros_constructor_refuses_float_and_bool():
    for v in (12.5, 12.0, True, False, "12", Decimal("12"), None):
        with pytest.raises(HaddonError) as ei:
            Micros(v)
        assert (err(ei).code, err(ei).status) == ("validation", 422), v
        assert err(ei).details[0]["reason"] == "int_required"
    assert Micros(12).value == 12  # positive control


def test_micros_parse_refuses_non_strings_and_non_integer_strings():
    for v in (12.5, 12, True, 12.0, Decimal("12"), b"12", None):
        with pytest.raises(HaddonError) as ei:
            Micros.parse(v)
        assert err(ei).code == "validation" and err(ei).details[0]["reason"] == "string_required", v
    for text in ("12.5", "12.0", "1e6", "+12", " 12", "12 ", "012", "-0", "", "1_000", "0x10", "１２"):
        with pytest.raises(HaddonError) as ei:
            Micros.parse(text)
        assert err(ei).code == "validation" and err(ei).details[0]["reason"] == "not_integer_string", text
    assert Micros.parse("0").value == 0 and Micros.parse("-12").value == -12 and Micros.parse(str(MAX_MICROS)).value == MAX_MICROS
    with pytest.raises(HaddonError) as ei:
        Micros.parse(str(MAX_MICROS + 1))
    assert (err(ei).code, err(ei).status) == ("overflow", 422)
    with pytest.raises(HaddonError) as ei:
        Micros.parse(str(-2**63))  # U6: excluded
    assert err(ei).code == "overflow"


def test_micros_from_decimal_refuses_floats_and_junk():
    for v in (12.5, 12.0, float("nan"), float("inf")):
        with pytest.raises(HaddonError) as ei:
            Micros.from_decimal(v)
        assert err(ei).code == "validation" and err(ei).details[0]["reason"] == "float_rejected", v
    for v in (12, True, None, [1]):
        with pytest.raises(HaddonError) as ei:
            Micros.from_decimal(v)
        assert err(ei).details[0]["reason"] == "decimal_required", v
    for text in ("1e6", "1E-6", "12.", ".5", "+1", "NaN", "Infinity", "1,5", " 1",
                 # the 0006 review: `$` also matches before a trailing newline, and `\d` is every Unicode
                 # decimal digit, so "12.34\n" and Arabic-Indic "١٢.٣٤" would both slip through as 12.34
                 "1 ", "1\n", "12.34\n", "\u0661\u0662", "\u0661\u0662.\u0663\u0664",
                 # and leading zeros, which the published contract refuses
                 "0012.00", "01", "000"):
        with pytest.raises(HaddonError) as ei:
            Micros.from_decimal(text)
        assert err(ei).details[0]["reason"] == "decimal_shape", text
    # zero has one spelling, on the inbound side as well as the outbound one
    for text in ("-0", "-0.0", "-0.00", "-0.000000"):
        with pytest.raises(HaddonError) as ei:
            Micros.from_decimal(text)
        assert err(ei).details[0]["reason"] == "negative_zero", text
    assert Micros.from_decimal("0").value == Micros.from_decimal("0.00").value == 0  # positive control
    # `Micros.parse`, the outbound form fed back in, is closed against the same `$` hole
    for text in ("12000000\n", "12000000 ", "\u0661\u0662"):
        with pytest.raises(HaddonError) as ei:
            Micros.parse(text)
        assert err(ei).details[0]["reason"] == "not_integer_string", text
    for d in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(HaddonError) as ei:
            Micros.from_decimal(d)
        assert err(ei).details[0]["reason"] == "non_finite"
    assert Micros.from_decimal("12.50").value == 12_500_000  # positive control
    assert Micros.from_decimal(Decimal("12.50")).value == 12_500_000


def test_micros_arithmetic_refuses_mixed_types_and_checks_range():
    with pytest.raises(TypeError):
        Micros(1) + 1
    with pytest.raises(TypeError):
        Micros(1) - 1.0
    with pytest.raises(HaddonError) as ei:
        Micros(MAX_MICROS) + Micros(1)
    assert err(ei).code == "overflow"
    with pytest.raises(HaddonError) as ei:
        Micros(MIN_MICROS) - Micros(1)  # -2^63 is excluded (U6)
    assert err(ei).code == "overflow"
    assert (-Micros(MIN_MICROS)).value == MAX_MICROS  # negation is always in range because -2^63 is excluded
    with pytest.raises(HaddonError) as ei:
        sum_micros([Micros(MAX_MICROS), Micros(1)])
    assert err(ei).code == "overflow"
    assert sum_micros([Micros(1), 2, Micros(-3)]).is_zero
    assert not hasattr(Micros, "__truediv__") and not hasattr(Micros, "__mul__") and not hasattr(Micros, "__float__")


# b. STRICT storage
# Setup rows come from chunk A's seeded template, so each INSERT is a known-good shape; the only thing that varies
# is the amount.
C = "entity_id,created_at,created_by"
CV = "'E','t','who'"
RAW_INSERTS = {
    "postings.amount_micros": f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES('fl',{CV},'e_draft',3,'cash','USD',?)",
    "allocations.amount_micros": f"INSERT INTO allocations(id,{C},explanation_id,line_id,posting_id,amount_micros) VALUES('fl',{CV},'x_partial','r2','e_spare.p1',?)",
    "statements.closing_micros": f"INSERT INTO statements(id,{C},document_id,account_id,commodity,period_start,period_end,opening_micros,closing_micros,locator,reason) VALUES('fl',{CV},'f4','cash','USD','2027-05-01','2027-05-31',0,?,'l','r')",
    "statement_lines.amount_micros": f"INSERT INTO statement_lines(id,{C},statement_id,line_no,source_key,content_hash,dt,amount_micros,description,fields,raw) VALUES('fl',{CV},'s1',9,'k9','h9','2026-03-15',?,'d','{{}}','raw')",
}
# 10000 micros = 0.01 USD is on the 2-decimal quantum, so the quantum trigger passes and STRICT is what refuses the
# fractional float.  (12.5 is refused too, but by the quantum trigger, which sees 12.5 % 10000 = 12 <> 0 first.)
ON_QUANTUM = 10000


@pytest.fixture
def raw(fresh_db):
    def _open() -> sqlite3.Connection:
        return fresh_db(locked=False)
    return _open


@pytest.mark.parametrize("column", sorted(RAW_INSERTS))
def test_raw_insert_of_fractional_float_is_refused_by_strict(raw, column):
    table, col = column.split(".")
    sql = RAW_INSERTS[column]
    con = raw()
    con.execute(sql, (ON_QUANTUM,))  # positive control: the integer shape is accepted
    assert con.execute(f"SELECT typeof({col}), {col} FROM {table} WHERE id='fl'").fetchone() == ("integer", ON_QUANTUM)
    con = raw()
    with pytest.raises(sqlite3.IntegrityError) as ei:
        con.execute(sql, (ON_QUANTUM + 0.5,))
    assert "cannot store REAL value in INTEGER column" in str(ei.value), str(ei.value)
    assert con.execute(f"SELECT count(*) FROM {table} WHERE id='fl'").fetchone() == (0,)
    con = raw()
    with pytest.raises(sqlite3.IntegrityError):  # 12.5: refused as well (quantum trigger or STRICT, whichever fires first)
        con.execute(sql, (12.5,))
    assert con.execute(f"SELECT count(*) FROM {table} WHERE id='fl'").fetchone() == (0,)


@pytest.mark.parametrize("column", sorted(RAW_INSERTS))
def test_raw_insert_of_integral_float_is_coerced_to_integer(raw, column):
    """Documented STRICT behaviour: a REAL that is exactly integral (10000.0) is stored as the INTEGER 10000.
    That is not a float surviving in the database - typeof() is 'integer' - but it is a contract to know about."""
    table, col = column.split(".")
    con = raw()
    con.execute(RAW_INSERTS[column], (float(ON_QUANTUM),))
    assert con.execute(f"SELECT typeof({col}), {col} FROM {table} WHERE id='fl'").fetchone() == ("integer", ON_QUANTUM)
    assert con.execute(f"SELECT count(*) FROM {table} WHERE typeof({col})='real'").fetchone() == (0,)


def test_strict_text_and_blob_contract(raw):
    """Documented STRICT contract for amount columns (measured, not assumed):
    - non-numeric TEXT and BLOB are refused;
    - TEXT that parses as a fractional number is refused (it would become REAL);
    - TEXT that parses as an integral number (' 10000', '1e4', '10000.0') is COERCED to INTEGER.
    The last point is why the API layer (Micros.parse) must be the gate against text amounts: the database is not."""
    sql = RAW_INSERTS["postings.amount_micros"]
    for bad, kind in (("abc", "TEXT"), (b"\x00\x01", "BLOB"), ("10000.5", "REAL"), ("1.5e4", "REAL")):
        con = raw()
        with pytest.raises(sqlite3.IntegrityError) as ei:
            con.execute(sql, (bad,))
        # the BEFORE INSERT quantum trigger runs before STRICT storage and may refuse first ('precision'); either way refused
        assert f"cannot store {kind} value in INTEGER column" in str(ei.value) or str(ei.value) == "precision", (bad, str(ei.value))
        assert con.execute("SELECT count(*) FROM postings WHERE id='fl'").fetchone() == (0,)
    # Every remaining amount column carries a quantum trigger, so to see STRICT's own refusal the probe has to be
    # an INTEGER column with no BEFORE INSERT trigger in front of it.  ``documents.size_bytes`` is exactly that:
    # an integer that is not money, on a table whose only triggers refuse UPDATE and DELETE.
    doc = f"INSERT INTO documents(id,{C},sha256,filename,media_type,size_bytes,blob_path) VALUES('fl',{CV},'s-fl','n','text/csv',?,'/z')"
    for bad, kind in (("abc", "TEXT"), (b"\x00\x01", "BLOB"), ("10000.5", "REAL"), (12.5, "REAL")):
        con = raw()
        with pytest.raises(sqlite3.IntegrityError) as ei:
            con.execute(doc, (bad,))
        assert f"cannot store {kind} value in INTEGER column documents.size_bytes" in str(ei.value), (bad, str(ei.value))
    con = raw()
    con.execute(doc, (ON_QUANTUM,))  # positive control for that statement
    assert con.execute("SELECT typeof(size_bytes) FROM documents WHERE id='fl'").fetchone() == ("integer",)
    for coerced in ("10000", " 10000", "1e4", "10000.0"):
        con = raw()
        con.execute(sql, (coerced,))
        assert con.execute("SELECT typeof(amount_micros), amount_micros FROM postings WHERE id='fl'").fetchone() == ("integer", ON_QUANTUM), coerced
    # ... and Micros.parse refuses every one of those spellings except the canonical one
    for text in (" 10000", "1e4", "10000.0", "10000.5", "abc"):
        with pytest.raises(HaddonError):
            Micros.parse(text)
    assert Micros.parse("10000").value == ON_QUANTUM


def test_every_amount_column_is_declared_integer_in_a_strict_table(migrated_template):
    con = sqlite3.connect(migrated_template)
    amount_cols = []
    for (table, sql) in con.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'"):
        assert sql.rstrip().upper().endswith("STRICT"), f"{table} is not STRICT"
        for row in con.execute(f'PRAGMA table_info("{table}")'):
            if row[1].endswith("_micros"):
                amount_cols.append(f"{table}.{row[1]}")
                assert row[2].upper() == "INTEGER", (table, row[1], row[2])
    con.close()
    assert sorted(amount_cols) == ["allocations.amount_micros",
                                   "postings.amount_micros", "statement_lines.amount_micros",
                                   "statements.closing_micros", "statements.opening_micros",
                                   # unrecovered tax: an amount the books did not recover, which is still an
                                   # amount and is held to the same rules (INTEGER, STRICT, a quantum trigger)
                                   "unrecovered_tax.amount_micros"]


# c. whole-database sweep
def realistic_sequence(fx) -> None:
    """Provisioning (already in fx) + drafts + postings + a reversal with replacement + request/operation +
    events + snapshot + close + reopen."""
    with fx.tx() as ctx:
        posted = entries.post_entry(ctx, dt="2026-03-10", narration="rent", origin="manual",
                                    legs=pair(fx, "125000000", memo="March rent", tax_code="VAT21"))
        entries.post_entry(ctx, dt="2026-03-11", narration="fx", origin="source",
                           legs=pair(fx, "1000000") + pair(fx, "2500000", commodity="EUR", credit="assets:cash-eur"))
        entries.post_entry(ctx, dt="2026-03-12", narration="yen", origin="source",
                           legs=[Leg(fx.acct("assets:cash-jpy"), "JPY", m("5000000000")), Leg(fx.acct("assets:cash-jpy"), "JPY", m("-5000000000"))])
        # Dated after the close below, deliberately.  The point of this entry is that a signed-64-bit extreme
        # reaches the database as an integer.  In March it would make the trial balance overflow, and a close
        # computes one (issue 0005), so the close would be refused.  That is right, but a different test.
        entries.post_entry(ctx, dt="2026-04-13", narration="extremes", origin="manual",
                           legs=[Leg(fx.acct("expenses:office"), "USD", m("9223372036854770000")), Leg(fx.acct("assets:cash"), "USD", m("-9223372036854770000"))])
        d = entries.create_draft(ctx, dt="2026-03-14", narration="scratch", origin="agent", legs=pair(fx, "10000"))
        entries.void(ctx, d.id, expected_version=1)
        entries.create_draft(ctx, dt="2026-03-14", narration="pending", origin="agent", legs=pair(fx, "20000"))
        op = requests.create_operation(ctx, kind="import", intent={"file": "a.csv"})
        op = requests.transition_operation(ctx, op.id, expected_version=1, state="running")
        requests.transition_operation(ctx, op.id, expected_version=2, state="succeeded", counts={"rows": 3}, result={"posted": 3})
        requests.record_request(ctx, caller=AGENT, intent_key="k1", method="POST", path="/v1/entries", request_hash="h1", http_status=201,
                                response={"id": posted.id, "amount": "125000000"}, operation_id=op.id)
        events.append_event(ctx, kind="entry.posted", subject_kind="entry", subject_id=posted.id, after={"amount_micros": "125000000"})
    with fx.tx(HUMAN) as ctx:
        corrections.reverse_entry(ctx, original_id=posted.id, dt="2026-03-20",
                                  reason="the rent figure was wrong",
                                  replacement=corrections.Replacement(dt="2026-03-20", narration="rent corrected", origin="adjustment",
                                                                      legs=pair(fx, "120000000", memo="March rent", tax_code="VAT21")))
    lock_books(fx, "2026-03-31")
    with fx.tx(HUMAN) as ctx:
        commands.reopen_period(ctx, through="2026-02-28", reason="late invoice", expected_version=2)


def sweep_for_reals(path) -> dict:
    """The whole-database sweep, reusable (chunk C1 runs it over the imported corpus Inc database): every column of every
    table is checked for ``typeof(col)='real'``.  Returns offenders plus what was inspected, so callers assert non-vacuity."""
    con = sqlite3.connect(path)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    inspected_tables, inspected_columns, inspected_rows, nonempty = 0, 0, 0, []
    offenders = []
    for table in tables:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
        n = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        inspected_tables += 1
        inspected_rows += n
        if n:
            nonempty.append(table)
        for col in cols:
            inspected_columns += 1
            bad = con.execute(f'SELECT count(*) FROM "{table}" WHERE typeof("{col}") = \'real\'').fetchone()[0]
            if bad:
                offenders.append(f"{table}.{col}: {bad} REAL value(s)")
    con.close()
    return {"offenders": offenders, "tables": inspected_tables, "columns": inspected_columns, "rows": inspected_rows, "nonempty": nonempty}


def test_no_real_anywhere_after_realistic_sequence(fx):
    realistic_sequence(fx)
    sweep = sweep_for_reals(fx.path)
    offenders, inspected_tables, inspected_columns, inspected_rows, nonempty = (
        sweep["offenders"], sweep["tables"], sweep["columns"], sweep["rows"], sweep["nonempty"])
    assert not offenders, "\n".join(offenders)
    # the sweep must have actually looked at something
    assert inspected_tables >= 20, inspected_tables
    assert inspected_columns >= 200, inspected_columns
    assert inspected_rows > 0
    assert len(nonempty) >= 15, nonempty
    for must in ("postings", "entries", "corrections", "close_records", "events", "requests", "operations", "snapshots"):
        assert must in nonempty, must
    assert fx.scalar("SELECT count(*) FROM postings") >= 14
    assert fx.scalar("SELECT max(abs(amount_micros)) FROM postings") == 9223372036854770000  # the extreme survived exactly


def test_sweep_would_catch_a_planted_real(fx):
    """Proves the sweep is not vacuous: plant a REAL where STRICT allows one (an ANY-free schema has none, so use a
    temporary non-STRICT table) and confirm the same typeof() query flags it."""
    con = fx.raw()
    con.execute("CREATE TEMP TABLE smuggle(x)")
    con.execute("INSERT INTO smuggle VALUES(12.5)")
    assert con.execute("SELECT count(*) FROM smuggle WHERE typeof(x)='real'").fetchone() == (1,)
    con.close()


# d. source tree
SRC = Path(__file__).resolve().parents[1] / "src" / "haddon"

# Allowlist of (relative path, line text fragment): each is a `/` between pathlib.Path objects (filesystem joins), not
# arithmetic.  There is no allowlisted float( or round( anywhere: the amount path has no legitimate use for either.
ALLOWLIST = {
    ("db/migrate.py", 'MIGRATIONS_DIR = Path(__file__).parent / "migrations"'),
    ("db/migrations/versions/0001_initial_schema.py", 'SQL_DIR = Path(__file__).resolve().parents[1] / "sql" / "0001"'),
    ("db/migrations/versions/0001_initial_schema.py", "data = (SQL_DIR / name).read_bytes()"),
}


def _scan(path: Path) -> list[tuple[int, str, str]]:
    """(lineno, kind, source line) for every true division, float()/round() call and float literal."""
    source = path.read_text()
    lines = source.splitlines()
    tree = ast.parse(source, str(path))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            hits.append((node.lineno, "true division", lines[node.lineno - 1].strip()))
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Div):
            hits.append((node.lineno, "true division", lines[node.lineno - 1].strip()))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"float", "round"}:
            hits.append((node.lineno, f"{node.func.id}()", lines[node.lineno - 1].strip()))
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            hits.append((node.lineno, "float literal", lines[node.lineno - 1].strip()))
    return hits


def test_src_is_float_free_on_the_amount_path():
    offenders = []
    seen_allowlisted = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        for lineno, kind, line in _scan(path):
            key = (rel, line)
            if key in ALLOWLIST:
                seen_allowlisted.add(key)
                continue
            offenders.append(f"{rel}:{lineno}: {kind}: {line}")
    assert not offenders, "float-shaped code in src/haddon (fix it or justify an allowlist entry):\n" + "\n".join(offenders)
    assert seen_allowlisted == ALLOWLIST, f"stale allowlist entries: {ALLOWLIST - seen_allowlisted}"


def test_src_scan_catches_the_shapes_we_care_about(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("a = 1 / 2\nb = float('1')\nc = round(x, 2)\nd = 1.5\ne = x / y\nq //= 2\nf /= 3\n")
    kinds = [k for _, k, _ in _scan(bad)]
    assert kinds.count("true division") == 3 and "float()" in kinds and "round()" in kinds and "float literal" in kinds
    ok = tmp_path / "ok.py"
    ok.write_text("a = 7 // 2\nb = divmod(7, 2)\nc = isinstance(x, float)\nd = 10 ** 6\ne = 'a/b'\n# 1 / 2 in a comment\n")
    assert _scan(ok) == []


def test_src_uses_no_sqlite_real_functions():
    """No SQL text in src/haddon reaches for REAL-producing SQLite functions on amounts."""
    import re
    pat = re.compile(r"\b(avg|round|total)\s*\(", re.IGNORECASE)
    hits = []
    for path in sorted(SRC.rglob("*")):
        if path.suffix in {".py", ".sql"} and path.is_file():
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if pat.search(line) and not line.lstrip().startswith(("#", "--")):
                    hits.append(f"{path.relative_to(SRC)}:{i}: {line.strip()}")
    assert not hits, "\n".join(hits)


# e. the decimal boundary
def test_decimal_boundary_is_exact():
    assert Micros.from_decimal("0.1") + Micros.from_decimal("0.2") == Micros.from_decimal("0.3")
    assert (Micros.from_decimal("0.1") + Micros.from_decimal("0.2")).value == 300000
    assert Micros.from_decimal("0.000001").value == 1 and Micros.from_decimal("-0.000001").value == -1
    assert Micros.from_decimal("1.10") == Micros.from_decimal("1.1") == Micros.from_decimal("1.100000")
    assert Micros.from_decimal("0.30").value - Micros.from_decimal("0.10").value == 200000
    assert Micros.from_decimal("9223372036854.775807").value == INT64_MAX
    assert Micros.from_decimal("-9223372036854.775807").value == -INT64_MAX


def test_seven_or_more_decimal_places_are_refused():
    for text in ("0.0000001", "1.2345678", "-0.0000005", "0.10000000"):
        with pytest.raises(HaddonError) as ei:
            Micros.from_decimal(text)
        e = err(ei)
        assert (e.code, e.status) == ("precision", 422) and e.details[0]["reason"] == "fractional_micros", text
    assert Micros.from_decimal("0.000001", commodity=XAU).value == 1  # positive control at exactly 6 places


def test_micros_fine_but_not_on_commodity_quantum_is_precision():
    with pytest.raises(HaddonError) as ei:
        Micros.from_decimal("12.345", commodity=USD)  # 3 places on a 2-decimal commodity
    assert err(ei).code == "precision" and err(ei).details[0]["reason"] == "unsupported_precision"
    with pytest.raises(HaddonError) as ei:
        Micros.from_decimal("12.500", commodity=USD)  # trailing zero still counts as a declared place (reader misconfigured)
    assert err(ei).details[0]["reason"] == "unsupported_precision"
    with pytest.raises(HaddonError) as ei:
        Micros(12_345_000).check_quantum(USD)  # 12.345 USD in micros: valid micros, not a multiple of 10000
    e = err(ei)
    assert (e.code, e.status) == ("precision", 422) and e.details[0]["reason"] == "quantum" and e.details[0]["quantum"] == 10000
    with pytest.raises(HaddonError) as ei:
        Micros(500_000).check_quantum(JPY)  # half a yen
    assert err(ei).code == "precision"
    assert Micros.from_decimal("12.34", commodity=USD).value == 12_340_000  # positive control
    assert Micros(12_340_000).check_quantum(USD).value == 12_340_000
    with pytest.raises(HaddonError) as ei:
        Micros.from_decimal(str(2**63), commodity=USD)
    assert err(ei).code == "overflow"


@pytest.mark.parametrize("text,commodity", [
    ("0.00", USD), ("0.01", USD), ("-0.01", USD), ("12.34", USD), ("-12.34", USD), ("1000000.00", USD),
    ("9223372036854.77", USD), ("-9223372036854.77", USD),  # the largest USD amounts on the quantum inside int64
    ("0", JPY), ("1", JPY), ("-1", JPY), ("9223372036854", JPY), ("-9223372036854", JPY),
    ("0.000000", XAU), ("0.000001", XAU), ("-0.000001", XAU), ("123.456789", XAU),
    ("9223372036854.775807", XAU), ("-9223372036854.775807", XAU),
])
def test_to_decimal_string_round_trips(text, commodity):
    mic = Micros.from_decimal(text, commodity=commodity)
    assert mic.to_decimal_string(commodity) == text
    assert Micros.from_decimal(mic.to_decimal_string(commodity), commodity=commodity) == mic
    assert Micros.parse(mic.render()) == mic
    assert isinstance(mic.value, int) and not isinstance(mic.value, bool)


def test_to_decimal_string_refuses_off_quantum_and_handles_negative_fractions():
    with pytest.raises(HaddonError) as ei:
        Micros(12_345_000).to_decimal_string(USD)
    assert err(ei).code == "precision"
    assert Micros(-10_000).to_decimal_string(USD) == "-0.01"
    assert Micros(-1).to_decimal_string(XAU) == "-0.000001"
    assert Micros(-1_000_000).to_decimal_string(JPY) == "-1"
    assert Micros(INT64_MAX).to_decimal_string(XAU) == "9223372036854.775807"
    assert Micros(-INT64_MAX).to_decimal_string(XAU) == "-9223372036854.775807"
