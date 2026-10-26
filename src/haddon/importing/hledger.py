"""The pinned-binary boundary around hledger.

Every hledger invocation goes through :class:`Hledger`, which re-verifies the binary (the ``--version`` string and
the sha256 in ``vendor/hledger.sha256``) before every run and refuses to run anything else.  A system-installed
hledger is never used: a different verifier is verifier drift.

The pin is per platform.  hledger's release binaries are not byte-identical across architectures, so the pin file
holds one ``<platform> <sha256>`` line per platform (``linux-x86_64``, ``mac-aarch64``).  The version must still be
``1.52.1``.  The official release reports it with a git-describe suffix (``1.52.1-g3834a163b-...``), which is the
same release and is accepted.  A platform with no pin is refused, not waved through.

Invocations use ``subprocess.run(check=True)``.  A nonzero exit becomes :class:`HledgerError` carrying the verbatim
stderr plus the file and line hledger names, never a summary.

JSON is parsed with strict shape validation: every key relied on must be present with the right type, or the
error names the transaction index and the missing path.  Amounts are taken from ``aquantity.decimalMantissa`` and
``aquantity.decimalPlaces`` only and converted exactly through ``Decimal``; the float member of ``aquantity`` is
never read (``json.loads`` is even told to turn every JSON float into a string so no float object exists), and
``tests/test_hledger_binary.py`` fails if its name appears anywhere in this package.

Declared commodities come from ``hledger commodities --declared``; their precision is read from the ``commodity``
directive's style in the journal files hledger reports (``hledger files``), never inferred from posting amounts.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import re
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from haddon.domain.micros import MAX_PRECISION, Commodity, Micros

PINNED_VERSION = "1.52.1"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BINARY = REPO_ROOT.joinpath("vendor", "hledger")
DEFAULT_PIN = REPO_ROOT.joinpath("vendor", "hledger.sha256")

_ERROR_HEAD = re.compile(r"^hledger: Error: (?P<file>.+?):(?P<line>\d+)(?:-\d+)?(?::\d+(?:-\d+)?)?:", re.MULTILINE)
_STATUSES = ("Unmarked", "Pending", "Cleared")
#: ``hledger 1.52.1, linux-x86_64`` or ``hledger 1.52.1-g3834a163b-20260428, mac-aarch64``
_VERSION = re.compile(r"^hledger\s+(?P<version>\d+(?:\.\d+)*)")


class HledgerBinaryError(RuntimeError):
    """The vendored binary is not the pinned one (missing, wrong sha256, wrong ``--version``)."""


class HledgerError(RuntimeError):
    """hledger exited nonzero.  ``stderr`` is verbatim; ``file`` / ``line`` come from its first error line, if any."""

    def __init__(self, command: list[str], returncode: int, stderr: str, stdout: str = "") -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        m = _ERROR_HEAD.search(stderr)
        self.file: str | None = m.group("file") if m else None
        self.line: int | None = int(m.group("line")) if m else None
        super().__init__(f"hledger exited {returncode}: {' '.join(command)}\n{stderr.rstrip()}")


class HledgerShapeError(ValueError):
    """The JSON hledger produced does not have the shape this importer relies on (names the transaction and path)."""

    def __init__(self, message: str, *, txn_index: int | None = None, path: str = "") -> None:
        self.txn_index = txn_index
        self.path = path
        where = f"transaction {txn_index}: " if txn_index is not None else ""
        super().__init__(f"{where}{path}: {message}" if path else f"{where}{message}")


class UnsupportedJournalFeature(HledgerShapeError):
    """A journal feature this importer cannot represent exactly (costs, virtual postings, posting dates...).
    Refused rather than silently dropped."""


@dataclass(frozen=True)
class Binary:
    path: Path
    version: str
    sha256: str


@dataclass(frozen=True)
class DeclaredCommodity:
    code: str
    precision: int
    directive_file: str
    directive_line: int

    def as_commodity(self) -> Commodity:
        return Commodity(self.code, self.precision)


@dataclass(frozen=True)
class Amount:
    commodity: str
    mantissa: int  # aquantity.decimalMantissa
    places: int  # aquantity.decimalPlaces
    style_precision: int  # astyle.asprecision: the commodity style hledger applied

    def decimal(self) -> Decimal:
        """``mantissa * 10**-places`` exactly (``Decimal.scaleb``; no division, no float)."""
        return Decimal(self.mantissa).scaleb(-self.places)

    def micros(self, declared: DeclaredCommodity) -> Micros:
        """Exact micros, refusing more than 6 places or a precision that differs from the declared style.

        Zero is the one exemption, and for an arithmetic reason: ``0`` scaled by any power of ten is still ``0``,
        so a zero amount's written precision carries no information.  The rule exists because ``EUR 1234.5``
        under a two-place declaration is ambiguous (1234.50, or a dropped digit?) and reading it either way would
        be a guess.  A zero admits no such reading.

        The case is real.  hledger's own idiom for a balance assertion is a zero posting carrying the asserted
        figure (``assets:cash  EUR 0 = EUR 1234.56``), and nobody writes the zero with two places;
        ``ExampleSL/2024`` does exactly this for its year-end assertions.  Refusing it made a journal that passes
        ``hledger check --strict`` unreadable, which is the wrong outcome for a rule about ambiguity.
        """
        if declared.code != self.commodity:
            raise HledgerShapeError(f"amount commodity {self.commodity!r} is not {declared.code!r}")
        if self.places > MAX_PRECISION:
            raise HledgerShapeError(f"{self.places} decimal places exceed the {MAX_PRECISION} micros carry")
        if self.mantissa == 0:
            return Micros.from_decimal(Decimal(0), commodity=declared.as_commodity(), path="aquantity")
        if self.places != declared.precision or self.style_precision != declared.precision:
            raise HledgerShapeError(
                f"{self.commodity} amount has decimalPlaces={self.places} / style precision {self.style_precision}; "
                f"the commodity directive declares {declared.precision}")
        return Micros.from_decimal(self.decimal(), commodity=declared.as_commodity(), path="aquantity")


@dataclass(frozen=True)
class Posting:
    account: str
    amount: Amount
    status: str
    comment: str
    tags: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Transaction:
    index: int  # tindex
    date: str
    description: str
    status: str
    code: str
    comment: str
    tags: tuple[tuple[str, str], ...]
    source_file: str  # tsourcepos[0].sourceName (absolute, as hledger reports it)
    source_line: int
    postings: tuple[Posting, ...]


def platform_key() -> str:
    """The pin-file key for this machine, in hledger's own vocabulary: ``linux-x86_64``, ``mac-aarch64``."""
    os_name = {"darwin": "mac", "linux": "linux"}.get(sys.platform, sys.platform)
    machine = {"arm64": "aarch64", "amd64": "x86_64"}.get(_platform.machine(), _platform.machine())
    return f"{os_name}-{machine}"


def read_pins(pin_file: Path) -> dict[str, str]:
    """Every ``<platform> <sha256>`` line in ``pin_file``, plus a bare digest as the ``'*'`` (any-platform) pin.

    Blank lines and ``#`` comments are ignored.  A malformed line (a token that is not a 64-hex digest) is an
    error, not a skipped line: a pin file that cannot be read is a gate that cannot be trusted.
    """
    pins: dict[str, str] = {}
    for i, raw in enumerate(Path(pin_file).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) == 1:
            key, token = "*", tokens[0]
        elif len(tokens) == 2:
            key, token = tokens
        else:
            raise HledgerBinaryError(f"{pin_file}:{i}: expected '<platform> <sha256>' or a bare sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise HledgerBinaryError(f"{pin_file}:{i}: does not hold a sha256 hex digest")
        if key in pins:
            raise HledgerBinaryError(f"{pin_file}:{i}: platform {key!r} is pinned twice")
        pins[key] = token
    return pins


def read_pin(pin_file: Path = DEFAULT_PIN, *, platform: str | None = None) -> str:
    """The sha256 pinned for this platform (or ``platform``), falling back to a bare any-platform pin.

    Refuses when the platform has no pin: a build nobody pinned is not the verifier.
    """
    key = platform if platform is not None else platform_key()
    pins = read_pins(pin_file)
    token = pins.get(key) or pins.get("*")
    if token is None:
        raise HledgerBinaryError(
            f"{pin_file}: no pin for platform {key!r} (pinned: {', '.join(sorted(pins)) or 'none'}); "
            f"add a '<platform> <sha256>' line for this build")
    return token


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _env() -> dict[str, str]:
    # hledger (GHC) decodes files with the locale encoding; force UTF-8 so journals with non-ASCII text are read
    # and error text is reproduced verbatim.  LEDGER_FILE is dropped: the journal is always passed with -f.
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_FILE"}
    env["LANG"] = env["LC_ALL"] = "C.UTF-8"
    return env


def resolve_binary(binary: Path = DEFAULT_BINARY, pin_file: Path = DEFAULT_PIN) -> Binary:
    """Verify the platform's pin (sha256) and the version; refuse anything but an hledger 1.52.1 for this platform."""
    binary, pin_file = Path(binary), Path(pin_file)
    if not binary.is_file():
        raise HledgerBinaryError(f"{binary}: pinned hledger binary is missing")
    if not pin_file.is_file():
        raise HledgerBinaryError(f"{pin_file}: sha256 pin file is missing")
    pin = read_pin(pin_file)
    digest = sha256_file(binary)
    if digest != pin:
        raise HledgerBinaryError(f"{binary}: sha256 {digest} does not match the pin {pin} in {pin_file}")
    try:
        proc = subprocess.run([str(binary), "--version"], capture_output=True, text=True, encoding="utf-8", env=_env(), check=False)
    except OSError as exc:
        raise HledgerBinaryError(f"{binary}: cannot execute: {exc}") from exc
    version_line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    m = _VERSION.match(version_line)
    if proc.returncode != 0 or m is None or m.group("version") != PINNED_VERSION:
        raise HledgerBinaryError(f"{binary}: --version reported {version_line!r}, expected hledger {PINNED_VERSION}")
    return Binary(path=binary, version=version_line, sha256=digest)


class Hledger:
    """All hledger invocations.  The binary is re-verified before every run (``resolve_binary``)."""

    def __init__(self, binary: Path = DEFAULT_BINARY, pin_file: Path = DEFAULT_PIN) -> None:
        self.binary = Path(binary)
        self.pin_file = Path(pin_file)

    def run(self, journal: Path, *args: str) -> str:
        """``hledger -f <abs journal> <args>`` with ``check=True``; returns stdout, raises :class:`HledgerError`."""
        binary = resolve_binary(self.binary, self.pin_file)  # the gate, every run
        journal = Path(journal)
        if not journal.is_absolute():
            raise ValueError(f"journal path must be absolute, got {journal}")
        if not journal.is_file():
            raise FileNotFoundError(f"journal {journal} does not exist")
        command = [str(binary.path), "-f", str(journal), *args]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=_env(), check=True)
        except subprocess.CalledProcessError as exc:
            raise HledgerError(command, exc.returncode, exc.stderr or "", exc.stdout or "") from None
        return proc.stdout

    def check_strict(self, journal: Path) -> None:
        self.run(journal, "check", "--strict")

    def print_json(self, journal: Path) -> list[Transaction]:
        text = self.run(journal, "print", "-O", "json")
        return parse_transactions(text)

    def declared_accounts(self, journal: Path) -> list[str]:
        out = self.run(journal, "accounts", "--declared")
        names = [line for line in out.splitlines() if line.strip()]
        if len(set(names)) != len(names):
            raise HledgerShapeError("hledger accounts --declared listed an account twice")
        return names

    def files(self, journal: Path) -> list[Path]:
        return [Path(line) for line in self.run(journal, "files").splitlines() if line.strip()]

    def declared_commodities(self, journal: Path) -> dict[str, DeclaredCommodity]:
        """Declared commodity symbols (``commodities --declared``) with the precision of their ``commodity``
        directive, located in the files hledger reports.  A declared symbol without a parseable directive is an error."""
        symbols = [line.strip() for line in self.run(journal, "commodities", "--declared").splitlines() if line.strip()]
        directives = read_commodity_directives(self.files(journal))
        out: dict[str, DeclaredCommodity] = {}
        for sym in symbols:
            if sym not in directives:
                raise HledgerShapeError(f"commodity {sym!r} is declared but no commodity directive with a style was found")
            out[sym] = directives[sym]
        return out


_DIRECTIVE = re.compile(r"^commodity\s+(?P<rest>.*?)\s*(?:;.*)?$")
_FORMAT = re.compile(r"^\s+format\s+(?P<rest>.*?)\s*(?:;.*)?$")
_NUMBER = re.compile(r"-?\d[\d.,\s]*|-?[.,]\d[\d.,\s]*")


def _strip_symbol(text: str) -> str:
    """Remove the commodity symbol (quoted or bare, either side) from a directive amount, leaving the number."""
    text = text.strip()
    if text.startswith('"'):
        text = text[text.index('"', 1) + 1:]
    elif text.endswith('"'):
        text = text[: text.rindex('"', 0, -1)]
    else:
        m = _NUMBER.search(text)
        if m is None:
            raise HledgerShapeError(f"commodity directive {text!r} has no number")
        text = m.group(0)
    return text.strip()


def precision_of_style(number: str) -> int:
    """Decimal places of a directive style number using hledger's rule: with two mark kinds, the last is the decimal
    mark; with one kind used once it is the decimal mark; with one kind used more than once it is a digit-group mark."""
    number = number.strip().lstrip("-").replace(" ", "")
    if not number or not re.fullmatch(r"[\d.,]+", number):
        raise HledgerShapeError(f"cannot read a style from {number!r}")
    marks = [(i, ch) for i, ch in enumerate(number) if ch in ".,"]
    if not marks:
        return 0
    kinds = {ch for _, ch in marks}
    last_i, last_ch = marks[-1]
    if len(kinds) == 1 and len(marks) > 1:
        return 0  # all group separators
    if len(kinds) == 2 and sum(1 for _, ch in marks if ch == last_ch) != 1:
        raise HledgerShapeError(f"ambiguous number style {number!r}")
    return len(number) - last_i - 1


def read_commodity_directives(files: list[Path]) -> dict[str, DeclaredCommodity]:
    """``commodity`` directives (single-line, or symbol-only followed by an indented ``format``) across ``files``.
    A ``decimal-mark`` directive is refused (unsupported here); a symbol declared twice must agree."""
    found: dict[str, DeclaredCommodity] = {}
    for path in files:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            if line.startswith("decimal-mark"):
                raise UnsupportedJournalFeature("decimal-mark directive", path=f"{path}:{i}")
            m = _DIRECTIVE.match(line)
            if not m:
                continue
            rest = m.group("rest")
            if not re.search(r"\d", rest):
                # Symbol only: the style is in an indented ``format`` subdirective below it.
                sym, number = rest.strip().strip('"'), None
                for j in range(i, min(i + 20, len(lines))):
                    fm = _FORMAT.match(lines[j])
                    if fm:
                        number = _strip_symbol(fm.group("rest"))
                        break
                    if lines[j].strip() and not lines[j][0].isspace():
                        break
                if number is None:
                    raise HledgerShapeError(f"commodity directive for {sym!r} has no style (no amount and no format subdirective)",
                                            path=f"{path}:{i}")
            else:
                number = _strip_symbol(rest)
                sym = _symbol_of(rest, number)
            precision = precision_of_style(number)
            decl = DeclaredCommodity(code=sym, precision=precision, directive_file=str(path), directive_line=i)
            if sym in found and found[sym].precision != precision:
                raise HledgerShapeError(f"commodity {sym!r} declared twice with different precisions", path=f"{path}:{i}")
            found.setdefault(sym, decl)
    return found


def _symbol_of(rest: str, number: str) -> str:
    rest = rest.strip()
    m = re.search(r'"([^"]+)"', rest)
    if m:
        return m.group(1)
    sym = rest.replace(number, "", 1).strip()
    if not sym:
        raise HledgerShapeError(f"commodity directive {rest!r} has no symbol")
    return sym


def _need(obj: Any, key: str, typ: type | tuple[type, ...], *, txn: int | None, path: str) -> Any:
    if not isinstance(obj, dict):
        raise HledgerShapeError("expected an object", txn_index=txn, path=path)
    if key not in obj:
        raise HledgerShapeError("missing", txn_index=txn, path=f"{path}.{key}" if path else key)
    v = obj[key]
    # bool is a subclass of int, so a JSON true would otherwise pass as a number.
    if isinstance(v, bool) and typ is int or not isinstance(v, typ):
        want = typ.__name__ if isinstance(typ, type) else "/".join(t.__name__ for t in typ)
        raise HledgerShapeError(f"expected {want}, got {type(v).__name__}", txn_index=txn, path=f"{path}.{key}" if path else key)
    return v


def _tags(raw: Any, *, txn: int, path: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        raise HledgerShapeError("expected a list of [name, value] pairs", txn_index=txn, path=path)
    out = []
    for i, pair in enumerate(raw):
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
            raise HledgerShapeError("expected a [name, value] string pair", txn_index=txn, path=f"{path}[{i}]")
        out.append((pair[0], pair[1]))
    return tuple(out)


def _amount(raw: Any, *, txn: int, path: str) -> Amount:
    commodity = _need(raw, "acommodity", str, txn=txn, path=path)
    if _need(raw, "acost", (dict, type(None)), txn=txn, path=path) is not None:
        raise UnsupportedJournalFeature("cost / price annotation on an amount", txn_index=txn, path=f"{path}.acost")
    q = _need(raw, "aquantity", dict, txn=txn, path=path)
    mantissa = _need(q, "decimalMantissa", int, txn=txn, path=f"{path}.aquantity")
    places = _need(q, "decimalPlaces", int, txn=txn, path=f"{path}.aquantity")
    style = _need(raw, "astyle", dict, txn=txn, path=path)
    precision = _need(style, "asprecision", int, txn=txn, path=f"{path}.astyle")
    if places < 0:
        raise HledgerShapeError("negative decimalPlaces", txn_index=txn, path=f"{path}.aquantity.decimalPlaces")
    return Amount(commodity=commodity, mantissa=mantissa, places=places, style_precision=precision)


def _posting(raw: Any, *, txn: int, path: str) -> Posting:
    account = _need(raw, "paccount", str, txn=txn, path=path)
    if not account:
        raise HledgerShapeError("empty account name", txn_index=txn, path=f"{path}.paccount")
    amounts = _need(raw, "pamount", list, txn=txn, path=path)
    if len(amounts) != 1:
        raise UnsupportedJournalFeature(f"posting carries {len(amounts)} amounts; exactly one is supported",
                                        txn_index=txn, path=f"{path}.pamount")
    status = _need(raw, "pstatus", str, txn=txn, path=path)
    if status not in _STATUSES:
        raise HledgerShapeError(f"unknown status {status!r}", txn_index=txn, path=f"{path}.pstatus")
    comment = _need(raw, "pcomment", str, txn=txn, path=path)
    tags = _tags(_need(raw, "ptags", list, txn=txn, path=path), txn=txn, path=f"{path}.ptags")
    ptype = _need(raw, "ptype", str, txn=txn, path=path)
    if ptype != "RegularPosting":
        raise UnsupportedJournalFeature(f"{ptype} (virtual postings are not representable)", txn_index=txn, path=f"{path}.ptype")
    for key in ("pdate", "pdate2"):
        if _need(raw, key, (str, type(None)), txn=txn, path=path) is not None:
            raise UnsupportedJournalFeature("posting-level date", txn_index=txn, path=f"{path}.{key}")
    return Posting(account=account, amount=_amount(amounts[0], txn=txn, path=f"{path}.pamount[0]"), status=status,
                   comment=comment, tags=tags)


def _transaction(raw: Any, position: int) -> Transaction:
    path = f"[{position}]"
    index = _need(raw, "tindex", int, txn=None, path=path)
    date = _need(raw, "tdate", str, txn=index, path=path)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HledgerShapeError(f"date {date!r} is not YYYY-MM-DD", txn_index=index, path=f"{path}.tdate")
    if _need(raw, "tdate2", (str, type(None)), txn=index, path=path) is not None:
        raise UnsupportedJournalFeature("secondary date", txn_index=index, path=f"{path}.tdate2")
    description = _need(raw, "tdescription", str, txn=index, path=path)
    status = _need(raw, "tstatus", str, txn=index, path=path)
    if status not in _STATUSES:
        raise HledgerShapeError(f"unknown status {status!r}", txn_index=index, path=f"{path}.tstatus")
    code = _need(raw, "tcode", str, txn=index, path=path)
    comment = _need(raw, "tcomment", str, txn=index, path=path)
    tags = _tags(_need(raw, "ttags", list, txn=index, path=path), txn=index, path=f"{path}.ttags")
    pos = _need(raw, "tsourcepos", list, txn=index, path=path)
    if not pos:
        raise HledgerShapeError("empty", txn_index=index, path=f"{path}.tsourcepos")
    source_file = _need(pos[0], "sourceName", str, txn=index, path=f"{path}.tsourcepos[0]")
    source_line = _need(pos[0], "sourceLine", int, txn=index, path=f"{path}.tsourcepos[0]")
    raw_postings = _need(raw, "tpostings", list, txn=index, path=path)
    if len(raw_postings) < 2:
        raise HledgerShapeError(f"{len(raw_postings)} posting(s); at least two are required", txn_index=index, path=f"{path}.tpostings")
    postings = tuple(_posting(p, txn=index, path=f"{path}.tpostings[{i}]") for i, p in enumerate(raw_postings))
    return Transaction(index=index, date=date, description=description, status=status, code=code, comment=comment,
                       tags=tags, source_file=source_file, source_line=source_line, postings=postings)


def parse_transactions(text: str) -> list[Transaction]:
    """Strictly parse ``hledger print -O json`` output.  JSON floats are turned into strings on load so no float
    object is ever created; transactions are returned sorted by ``tindex``, which must be unique."""
    try:
        data = json.loads(text, parse_float=str)
    except json.JSONDecodeError as exc:
        raise HledgerShapeError(f"hledger print -O json output is not JSON: {exc}") from None
    if not isinstance(data, list):
        raise HledgerShapeError("top level is not a list of transactions")
    txns = [_transaction(raw, i) for i, raw in enumerate(data)]
    indexes = [t.index for t in txns]
    if len(set(indexes)) != len(indexes):
        raise HledgerShapeError("tindex values are not unique")
    return sorted(txns, key=lambda t: t.index)
