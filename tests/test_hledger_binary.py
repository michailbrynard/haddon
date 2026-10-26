"""The pinned-binary boundary (chunk C1): the gate on ``vendor/hledger``, verbatim error carry-through, strict JSON
shape validation, exact amounts from the decimal mantissa, and declarations read from directives."""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from haddon.domain.micros import Micros
from haddon.importing import hledger as H
from haddon.importing.hledger import (DEFAULT_BINARY, DEFAULT_PIN, PINNED_VERSION, Amount, DeclaredCommodity, Hledger,
                                      HledgerBinaryError, HledgerError, HledgerShapeError, UnsupportedJournalFeature,
                                      parse_transactions, platform_key, precision_of_style, read_pin, read_pins,
                                      resolve_binary)
from haddon.importing.corpus import load_corpus

CORPUS = load_corpus()

IMPORTING_SRC = Path(__file__).resolve().parents[1] / "src" / "haddon" / "importing"
SPAIN = CORPUS.journal("ExampleSL")
BV = CORPUS.journal("ExampleBV")
INC = CORPUS.journal("ExampleInc")


# 1. the binary gate
@pytest.mark.slow
def test_pinned_binary_is_1_52_1_with_the_pinned_sha256():
    b = resolve_binary()
    assert b.path == DEFAULT_BINARY
    assert b.version.startswith(f"hledger {PINNED_VERSION}"), b.version
    assert PINNED_VERSION == "1.52.1"
    assert b.sha256 == read_pin(DEFAULT_PIN)
    # The real file agrees with an independent sha256.
    assert H.sha256_file(DEFAULT_BINARY) == read_pin(DEFAULT_PIN)


def test_the_pin_is_per_platform_and_an_unlisted_platform_is_refused():
    """A release binary is not byte-identical across architectures: each platform has its own sha256, and a
    platform nobody pinned is refused rather than run."""
    pins = read_pins(DEFAULT_PIN)
    assert pins[platform_key()] == read_pin(DEFAULT_PIN)
    assert read_pin(DEFAULT_PIN, platform="linux-x86_64") == pins["linux-x86_64"]
    with pytest.raises(HledgerBinaryError, match="no pin for platform"):
        read_pin(DEFAULT_PIN, platform="plan9-sparc")


def test_a_platform_pin_file_rejects_malformed_lines(tmp_path):
    p = tmp_path / "pins"
    p.write_text("# comment\n\nlinux-x86_64 " + "a" * 64 + "\n")
    assert read_pins(p) == {"linux-x86_64": "a" * 64}
    p.write_text("linux-x86_64 not-a-digest\n")
    with pytest.raises(HledgerBinaryError, match="sha256 hex digest"):
        read_pins(p)
    p.write_text("a b c\n")
    with pytest.raises(HledgerBinaryError, match="expected"):
        read_pins(p)
    p.write_text("linux-x86_64 " + "a" * 64 + "\nlinux-x86_64 " + "b" * 64 + "\n")
    with pytest.raises(HledgerBinaryError, match="pinned twice"):
        read_pins(p)


def test_system_hledger_is_not_the_pinned_binary():
    system = shutil.which("hledger")
    if system is None:
        pytest.skip("no system hledger on this box")
    assert Path(system) != DEFAULT_BINARY
    with pytest.raises(HledgerBinaryError):
        resolve_binary(Path(system), DEFAULT_PIN)  # drift: a different build, refused


@pytest.mark.slow
def test_tampered_pin_makes_the_gate_fail(tmp_path):
    tampered = tmp_path / "hledger.sha256"
    tampered.write_text("0" * 64 + "\n")  # a bare, wrong sha256 applies to every platform
    with pytest.raises(HledgerBinaryError, match="does not match the pin"):
        resolve_binary(DEFAULT_BINARY, tampered)
    # The gate runs before every invocation: nothing runs with a bad pin.
    with pytest.raises(HledgerBinaryError):
        Hledger(pin_file=tampered).check_strict(BV)
    for junk, match in (("", "no pin for platform"), ("not-a-digest\n", "sha256 hex digest"), ("abc\n", "sha256 hex digest")):
        tampered.write_text(junk)
        with pytest.raises(HledgerBinaryError, match=match):
            resolve_binary(DEFAULT_BINARY, tampered)


def test_tampered_binary_fails_on_sha_and_a_wrong_version_fails_on_version(tmp_path):
    fake = tmp_path / "hledger"
    fake.write_text('#!/bin/sh\necho "hledger 1.52.1, linux-x86_64"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(HledgerBinaryError, match="does not match the pin"):
        resolve_binary(fake, DEFAULT_PIN)  # a substituted binary never matches the real pin
    # A binary whose pin matches but which is not 1.52.1 is refused on the version string.
    drift = tmp_path / "hledger-1.30"
    drift.write_text('#!/bin/sh\necho "hledger 1.30.1, linux-x86_64"\n')
    drift.chmod(drift.stat().st_mode | stat.S_IXUSR)
    pin = tmp_path / "drift.sha256"
    pin.write_text(H.sha256_file(drift) + "\n")
    with pytest.raises(HledgerBinaryError, match="expected hledger 1.52.1"):
        resolve_binary(drift, pin)
    with pytest.raises(HledgerBinaryError, match="missing"):
        resolve_binary(tmp_path / "nope", DEFAULT_PIN)


def test_the_official_release_version_suffix_is_accepted(tmp_path):
    """hledger's own release binaries report ``1.52.1-g3834a163b-20260428`` (a git-describe suffix), which is the
    same 1.52.1: the version gate matches the number, not the exact line."""
    mac = tmp_path / "hledger-mac"
    mac.write_text('#!/bin/sh\necho "hledger 1.52.1-g3834a163b-20260428, mac-aarch64"\n')
    mac.chmod(mac.stat().st_mode | stat.S_IXUSR)
    pin = tmp_path / "mac.sha256"
    pin.write_text(H.sha256_file(mac) + "\n")
    assert resolve_binary(mac, pin).version.startswith("hledger 1.52.1-")
    # A 1.52.3 (a different release) is still refused.
    newer = tmp_path / "hledger-newer"
    newer.write_text('#!/bin/sh\necho "hledger 1.52.3, mac-aarch64"\n')
    newer.chmod(newer.stat().st_mode | stat.S_IXUSR)
    pin.write_text(H.sha256_file(newer) + "\n")
    with pytest.raises(HledgerBinaryError, match="expected hledger 1.52.1"):
        resolve_binary(newer, pin)


# Stderr carried through
@pytest.mark.slow
def test_nonzero_exit_carries_the_real_stderr_file_and_line(tmp_path):
    """A real nonzero hledger exit with real stderr, not a synthesised one.

    The failing journal is the stand-in ``tests/test_import_spain.blocked_checkout`` builds: the S.L.'s journals
    minus their ``commodity EUR`` directives."""
    from test_import_spain import blocked_checkout
    main = blocked_checkout(tmp_path) / "ExampleSL" / "2025" / "ledger" / "2025.journal"
    assert Hledger().check_strict(SPAIN) is None  # the real source passes; the stand-in is what fails
    with pytest.raises(HledgerError) as ei:
        Hledger().check_strict(main)
    e = ei.value
    assert e.returncode == 1
    assert 'commodity "EUR" has not been declared' in e.stderr
    assert "Strict commodity checking is enabled" in e.stderr
    assert e.file == str(main.parent / "2025-opening.journal") and e.line == 9
    assert e.stderr.rstrip() in str(e)  # verbatim, not summarised
    assert e.command[:3] == [str(DEFAULT_BINARY), "-f", str(main)] and e.command[3:] == ["check", "--strict"]


@pytest.mark.slow
def test_journal_path_must_be_absolute_and_exist(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        Hledger().run(Path("relative.journal"), "check")
    with pytest.raises(FileNotFoundError):
        Hledger().run(tmp_path / "missing.journal", "check")


# No float on the path
def test_the_float_member_of_aquantity_is_never_named_in_the_importing_package():
    needle = "floating" + "Point"  # split so this test file is the only place the name appears
    hits = [f"{p.relative_to(IMPORTING_SRC)}:{i}" for p in sorted(IMPORTING_SRC.rglob("*.py"))
            for i, line in enumerate(p.read_text().splitlines(), 1) if needle in line]
    assert not hits, hits
    assert sum(1 for _ in IMPORTING_SRC.rglob("*.py")) >= 3  # the scan looked at the package


def test_json_floats_become_strings_on_load_so_no_float_object_exists():
    raw = [_txn_json(mantissa=1005, places=2, extra_quantity={"floating" + "Point": 10.05})]
    txns = parse_transactions(json.dumps(raw))
    a = txns[0].postings[0].amount
    assert (a.mantissa, a.places) == (1005, 2)
    assert not any(isinstance(v, float) for v in vars(a).values())


def test_micros_come_from_decimal_mantissa_and_places_exactly():
    usd = DeclaredCommodity("USD", 2, "x.journal", 1)
    # -512.17 USD: the float route rounds to -512169999.99999994 -> int() would give -512169999
    assert Amount("USD", -51217, 2, 2).micros(usd) == Micros(-512170000)
    assert int(-51217 / 100 * 10**6) != -512170000  # the defect the exact path avoids (float used only in this test)
    assert Amount("USD", -51217, 2, 2).decimal() == Decimal("-512.17")
    assert Amount("USD", 0, 2, 2).micros(usd) == Micros(0)
    assert Amount("USD", 87654321, 2, 2).micros(usd) == Micros(876543210000)
    # hledger's max precision marker, or anything past 6 places, is refused
    with pytest.raises(HledgerShapeError, match="exceed"):
        Amount("USD", 1, 7, 7).micros(DeclaredCommodity("USD", 7, "x", 1))
    with pytest.raises(HledgerShapeError, match="exceed"):
        Amount("USD", 1, 255, 255).micros(usd)
    # a posting whose precision does not match the declared style is refused (both directions)
    with pytest.raises(HledgerShapeError, match="declares 2"):
        Amount("USD", 1005, 3, 3).micros(usd)
    with pytest.raises(HledgerShapeError, match="declares 2"):
        Amount("USD", 10, 1, 1).micros(usd)
    with pytest.raises(HledgerShapeError, match="declares 2"):
        Amount("USD", 1005, 2, 3).micros(usd)  # style drifted even though places agree
    with pytest.raises(HledgerShapeError, match="is not"):
        Amount("EUR", 100, 2, 2).micros(usd)


def test_a_zero_is_read_at_whatever_precision_it_was_written_at():
    """Zero is the one amount whose written precision carries no information: ``0`` scaled by any power of ten
    is ``0``, so the style rule has nothing to protect.

    hledger's own idiom for a balance assertion is a zero posting carrying the asserted figure
    (``assets:cash  EUR 0 = EUR 1234.56``), and nobody writes that zero with two decimal places.  Refusing it
    would make a journal that passes ``hledger check --strict`` unreadable; ``ExampleSL/2024`` is one.  Every
    non-zero mismatch above is still refused.
    """
    usd = DeclaredCommodity("USD", 2, "x.journal", 1)
    assert Amount("USD", 0, 0, 0).micros(usd) == Micros(0)
    assert Amount("USD", 0, 2, 2).micros(usd) == Micros(0)
    assert Amount("USD", 0, 4, 0).micros(usd) == Micros(0)
    # The carry limit is arithmetic, not stylistic, so it still applies.
    with pytest.raises(HledgerShapeError, match="exceed"):
        Amount("USD", 0, 7, 0).micros(usd)
    # The commodity still has to be the declared one.
    with pytest.raises(HledgerShapeError, match="is not"):
        Amount("EUR", 0, 0, 0).micros(usd)


@pytest.mark.slow
def test_a_zero_balance_assertion_posting_is_read_by_the_gate(tmp_path):
    """End to end on the real binary: the shape `ExampleSL/2024` writes its year-end assertions in."""
    j = tmp_path / "a.journal"
    j.write_text("commodity EUR 1,000.00\naccount assets:cash:x\naccount expenses:y\n\n"
                 "2024-12-31 * asserted\n    assets:cash:x    EUR 0 = EUR 0.00\n"
                 "    expenses:y    EUR 0 = EUR 0.00\n")
    hl = Hledger()
    hl.check_strict(j)
    txns = hl.print_json(j)
    declared = hl.declared_commodities(j)
    amount = txns[0].postings[0].amount
    assert (amount.mantissa, amount.places) == (0, 0)     # hledger reports it exactly as written
    assert amount.micros(declared["EUR"]) == Micros(0)


@pytest.mark.slow
def test_three_place_amount_under_a_two_place_directive_is_refused_by_the_gate(tmp_path):
    j = tmp_path / "a.journal"
    j.write_text("commodity USD 1,000.00\naccount assets:cash:x\naccount expenses:y\n\n2025-01-02 * three places\n"
                 "    expenses:y    1.005 USD\n    assets:cash:x\n")
    hl = Hledger()
    hl.check_strict(j)  # hledger itself accepts it
    txns = hl.print_json(j)
    declared = hl.declared_commodities(j)
    assert declared["USD"].precision == 2
    with pytest.raises(HledgerShapeError, match="decimalPlaces=3"):
        txns[0].postings[0].amount.micros(declared["USD"])


# Strict JSON shape
def _txn_json(*, mantissa=100, places=2, extra_quantity=None, **overrides):
    q = {"decimalMantissa": mantissa, "decimalPlaces": places, **(extra_quantity or {})}
    amount = {"acommodity": "USD", "acost": None, "acostbasis": None, "aquantity": q,
              "astyle": {"ascommodityside": "L", "ascommodityspaced": True, "asdecimalmark": ".", "asdigitgroups": None,
                         "asprecision": places, "asrounding": "NoRounding"}}
    neg = json.loads(json.dumps(amount))
    neg["aquantity"]["decimalMantissa"] = -mantissa

    def posting(acct, amt):
        return {"paccount": acct, "pamount": [amt], "pbalanceassertion": None, "pcomment": "", "pdate": None, "pdate2": None,
                "poriginal": None, "pstatus": "Unmarked", "ptags": [], "ptransaction_": "1", "ptype": "RegularPosting"}

    t = {"tcode": "", "tcomment": "", "tdate": "2025-01-02", "tdate2": None, "tdescription": "d", "tindex": 1,
         "tpostings": [posting("expenses:y", amount), posting("assets:cash:x", neg)], "tprecedingcomment": "",
         "tsourcepos": [{"sourceColumn": 1, "sourceLine": 5, "sourceName": "/x/a.journal"}], "tstatus": "Cleared", "ttags": []}
    t.update(overrides)
    return t


def test_well_formed_json_parses():
    [t] = parse_transactions(json.dumps([_txn_json()]))
    assert (t.index, t.date, t.description, t.status, t.source_file, t.source_line) == (1, "2025-01-02", "d", "Cleared", "/x/a.journal", 5)
    assert [p.account for p in t.postings] == ["expenses:y", "assets:cash:x"]
    assert [p.amount.mantissa for p in t.postings] == [100, -100]


@pytest.mark.parametrize("mutate,match", [
    (lambda t: t.pop("tdate"), r"transaction 1: \[0\]\.tdate: missing"),
    (lambda t: t.pop("tdescription"), r"\[0\]\.tdescription: missing"),
    (lambda t: t.pop("tstatus"), r"tstatus: missing"),
    (lambda t: t.pop("tcode"), r"tcode: missing"),
    (lambda t: t.pop("tcomment"), r"tcomment: missing"),
    (lambda t: t.pop("tindex"), r"\[0\]\.tindex: missing"),
    (lambda t: t.pop("tsourcepos"), r"tsourcepos: missing"),
    (lambda t: t.pop("tpostings"), r"tpostings: missing"),
    (lambda t: t.__setitem__("tindex", "1"), r"tindex: expected int, got str"),
    (lambda t: t.__setitem__("tindex", True), r"tindex: expected int, got bool"),
    (lambda t: t.__setitem__("tdate", "02/01/2025"), r"tdate.*not YYYY-MM-DD"),
    (lambda t: t.__setitem__("tstatus", "Posted"), r"unknown status"),
    (lambda t: t["tpostings"].pop(), r"tpostings: 1 posting"),
    (lambda t: t["tpostings"][0].pop("paccount"), r"tpostings\[0\]\.paccount: missing"),
    (lambda t: t["tpostings"][1].pop("pamount"), r"tpostings\[1\]\.pamount: missing"),
    (lambda t: t["tpostings"][0].pop("pstatus"), r"pstatus: missing"),
    (lambda t: t["tpostings"][0].pop("pcomment"), r"pcomment: missing"),
    (lambda t: t["tpostings"][0].pop("ptags"), r"ptags: missing"),
    (lambda t: t["tpostings"][0].__setitem__("ptags", [["only-name"]]), r"ptags\[0\]"),
    (lambda t: t["tpostings"][0]["pamount"][0]["aquantity"].pop("decimalMantissa"), r"aquantity\.decimalMantissa: missing"),
    (lambda t: t["tpostings"][0]["pamount"][0]["aquantity"].pop("decimalPlaces"), r"aquantity\.decimalPlaces: missing"),
    (lambda t: t["tpostings"][0]["pamount"][0]["aquantity"].__setitem__("decimalMantissa", "100"), r"decimalMantissa: expected int"),
    (lambda t: t["tpostings"][0]["pamount"][0]["aquantity"].__setitem__("decimalMantissa", 100.0), r"decimalMantissa: expected int, got str"),
    (lambda t: t["tpostings"][0]["pamount"][0].pop("astyle"), r"astyle: missing"),
    (lambda t: t["tpostings"][0]["pamount"][0].pop("acommodity"), r"acommodity: missing"),
])
def test_missing_or_mistyped_keys_name_the_transaction_and_path(mutate, match):
    t = _txn_json()
    mutate(t)
    with pytest.raises(HledgerShapeError, match=match):
        parse_transactions(json.dumps([t]))


@pytest.mark.parametrize("mutate,match", [
    (lambda t: t["tpostings"][0]["pamount"][0].__setitem__("acost", {"some": "cost"}), "cost"),
    (lambda t: t["tpostings"][0]["pamount"].append(t["tpostings"][0]["pamount"][0]), "2 amounts"),
    (lambda t: t["tpostings"][0].__setitem__("ptype", "VirtualPosting"), "VirtualPosting"),
    (lambda t: t["tpostings"][0].__setitem__("pdate", "2025-01-03"), "posting-level date"),
    (lambda t: t.__setitem__("tdate2", "2025-01-03"), "secondary date"),
])
def test_unrepresentable_journal_features_are_refused_not_dropped(mutate, match):
    t = _txn_json()
    mutate(t)
    with pytest.raises(UnsupportedJournalFeature, match=match):
        parse_transactions(json.dumps([t]))


def test_top_level_shape_and_duplicate_tindex():
    with pytest.raises(HledgerShapeError, match="not JSON"):
        parse_transactions("{")
    with pytest.raises(HledgerShapeError, match="not a list"):
        parse_transactions("{}")
    with pytest.raises(HledgerShapeError, match="not unique"):
        parse_transactions(json.dumps([_txn_json(), _txn_json()]))
    a, b = _txn_json(tindex=2), _txn_json(tindex=1)
    assert [t.index for t in parse_transactions(json.dumps([a, b]))] == [1, 2]  # sorted by tindex, not print order


# Declarations
@pytest.mark.slow
def test_declared_commodities_come_from_the_directive_style():
    hl = Hledger()
    bv = hl.declared_commodities(BV)
    assert set(bv) == {"EUR"} and bv["EUR"].precision == 2
    assert bv["EUR"].directive_file == str(BV) and bv["EUR"].directive_line == 10
    inc = hl.declared_commodities(INC)
    assert set(inc) == {"USD"} and inc["USD"].precision == 2 and inc["USD"].directive_line == 10
    # The S.L. declares EUR twice, in 2025.journal and in the accounts.journal it includes.  Both styles mean
    # precision 2, so they agree and the first file hledger reports is the one located.  A disagreement would be
    # a HledgerShapeError, not a silent win.
    es = hl.declared_commodities(SPAIN)
    assert set(es) == {"EUR"} and es["EUR"].precision == 2
    assert es["EUR"].directive_file == str(SPAIN) and es["EUR"].directive_line == 16
    assert SPAIN.read_text().splitlines()[15] == "commodity EUR 1,000.00"


@pytest.mark.parametrize("style,precision", [("1,000.00", 2), ("1.000,00", 2), ("1000", 0), ("1000.", 0), ("1,000", 3),
                                             ("1.000.000", 0), ("1 000,00", 2), ("0.000000", 6), ("-1,234.5", 1)])
def test_precision_of_style_follows_hledgers_rule(style, precision):
    assert precision_of_style(style) == precision


def test_directive_parser_handles_symbol_sides_quotes_and_format_subdirective(tmp_path):
    j = tmp_path / "d.journal"
    j.write_text('commodity USD 1,000.00\ncommodity 1.000,00 EUR ; comment\ncommodity "AAPL" 1.0000\n'
                 'commodity JPY\n  format 1,000 JPY\ncommodity XAU\n  format XAU 1.000000\n')
    got = H.read_commodity_directives([j])
    assert {k: v.precision for k, v in got.items()} == {"USD": 2, "EUR": 2, "AAPL": 4, "JPY": 3, "XAU": 6}
    j.write_text("decimal-mark ,\ncommodity USD 1.000,00\n")
    with pytest.raises(UnsupportedJournalFeature, match="decimal-mark"):
        H.read_commodity_directives([j])
    j.write_text("commodity USD 1,000.00\ncommodity USD 1,000.000\n")
    with pytest.raises(HledgerShapeError, match="different precisions"):
        H.read_commodity_directives([j])
    j.write_text("commodity USD\n\n2025-01-01 x\n")
    with pytest.raises(HledgerShapeError, match="no style"):
        H.read_commodity_directives([j])


@pytest.mark.slow
def test_declared_accounts_are_the_chart():
    """What ``declared_accounts`` returns is a chart: unique, sorted into the five top-level types, and holding
    the retained-earnings account every book needs.  No count of anybody's accounts is asserted (chunk G1)."""
    hl = Hledger()
    inc = hl.declared_accounts(INC)
    assert inc and len(inc) == len(set(inc))
    assert {a.split(":", 1)[0] for a in inc} == {"assets", "liabilities", "equity", "income", "expenses"}
    assert "equity:retained-earnings" in inc
    bv = hl.declared_accounts(BV)
    assert bv and len(bv) == len(set(bv)) and "equity:retained-earnings" in bv
    files = hl.files(BV)
    assert files[0] == BV and len(files) > 1 and all(f.is_absolute() for f in files)
