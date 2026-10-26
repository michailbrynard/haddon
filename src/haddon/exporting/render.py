"""Deterministic rendering of one book into hledger journal text (chunk C2a).

Everything here is pure: a :class:`BookState` read out of the store goes in, a ``{filename: text}`` mapping comes
out.  Nothing consults the clock, the filesystem, ``uuid`` or a ``set`` iteration order, so two renders of the same
state are the same bytes in this process, in the next one, and on another machine.

The rules that make that true, all fixed here and nowhere else:

* Transaction order is ``(dt, entry_id)``.  The entry id is unique, so the order is total and needs no tie-break.
  The import mints ``e-<year>-<tindex:06d>``, so for imported entries this is source order within a date; entries
  minted in the store afterwards sort by their own id.  Postings keep their stored ``seq``.
* Accounts are ordered byte-wise by path, commodities by code, and files by filename, with ``accounts.journal``
  and the opening group included first.
* Amounts are rendered by :meth:`Micros.to_decimal_string` from the integer micros and the commodity's declared
  precision: exact, or a ``precision`` error.  No float and no stored display string is involved.
* Column widths are computed from the file's own contents (longest account path, longest amount), so they are a
  function of the state, not of insertion order.

Content the journal format cannot carry back losslessly is refused (:class:`UnrenderableContent`), never silently
mangled: a narration holding ``;`` or a newline, an account path holding a comment character or a double space, a
commodity symbol that would need escaping we do not do, a source filename that is not ``<year>-<group>.journal``.

The transaction-level note fields (``entries.code`` / ``comment`` / ``tags``) came with chunk E1 and migration
0002.  The code is written as ``(code)`` after the status mark.  The comment is written the way hledger writes one:
the first of its ``\n``-separated lines on the transaction's own line, the rest as comment lines above the first
posting.  The tags are not written separately at all, because hledger derives them by parsing that comment.  Two
refusals follow from getting that exactly right: a narration starting with ``(`` (a reparse would read it as the
code) and a tag whose name the comment does not contain (there would be nowhere to write it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from haddon.domain.canonical import Tag, canonical_json, sha256_hex
from haddon.domain.micros import Commodity, Micros

SCHEMA = "haddon-export-1"
INDENT = "    "  # posting indent
MEMO_INDENT = "        "  # continuation comment lines, indented past the posting
GAP = "  "  # the two spaces that end an account name in hledger
OPENING_GROUP = "opening"
STORE_GROUP = "haddon"  # entries with no journal_mappings row (reversals, manual postings): minted here, not imported
MARK_BY_SOURCE_STATUS = {"Cleared": "*", "Pending": "!", "Unmarked": ""}
POSTED_MARK = "*"  # a posted entry with no source status of its own

_SIMPLE_SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_GROUP = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")  # also the path guard: no '/', no '..', no spaces
_BAD_IN_TEXT = ("\n", "\r", ";")


class UnrenderableContent(ValueError):
    """Book content the hledger journal format cannot carry back exactly.  Refused, never mangled."""

    def __init__(self, message: str, *, path: str = "", value: object = None) -> None:
        self.path = path
        self.value = value
        super().__init__(f"{path}: {message}" if path else message)


@dataclass(frozen=True)
class RPosting:
    account: str
    commodity: str
    amount: Micros
    memo: str | None


@dataclass(frozen=True)
class RTransaction:
    entry_id: str
    dt: str
    narration: str
    mark: str  # '', '*' or '!'
    origin: str
    group: str  # the file it goes to: <year>-<group>.journal
    postings: tuple[RPosting, ...]
    code: str = ""  # hledger tcode, written as `(code)` after the status mark
    comment: str = ""  # hledger tcomment: element 0 of its lines goes on the transaction line, the rest above the legs
    tags: tuple[Tag, ...] = ()  # hledger ttags, which hledger re-derives from the comment when this file is reparsed

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.dt, self.entry_id)


@dataclass(frozen=True)
class BookState:
    """Everything the export renders, already scoped to one entity and one date range."""

    entity_id: str
    entity_name: str
    entity_dir: str  # the directory name the artifacts go under
    start: str       # the inclusive first day of the range
    end: str         # the inclusive last day of the range
    source_revision: str
    commodities: tuple[Commodity, ...]  # by code
    accounts: tuple[str, ...]  # declared chart, by path
    transactions: tuple[RTransaction, ...]  # by (dt, entry_id)

    @property
    def label(self) -> str:
        """The short name of the range, used for filenames: the year when the range is a whole calendar year,
        and its two dates otherwise.  A journal is continuous (issue 0002), so the label is a convenience for a
        file name rather than a storage identity."""
        if self.start is None or self.end is None:
            return self.entity_id
        if self.start[:4] == self.end[:4] and self.start[4:] == "-01-01" and self.end[4:] == "-12-31":
            return self.start[:4]
        return f"{self.start}_{self.end}"

    @property
    def posting_count(self) -> int:
        return sum(len(t.postings) for t in self.transactions)


def group_for(journal_path: str) -> str:
    """The source group of a ``journal_mappings.journal_path``: ``.../<year>-<group>.journal`` -> ``<group>``.

    The group becomes a filename, so it is validated to a lowercase ``a-z0-9-`` word: a path separator, ``..`` or a
    space in a stored source path can never become a write outside the managed directory."""
    name = PurePosixPath(journal_path).name
    match = re.fullmatch(r"\d{4}-(.+)\.journal", name)
    if match is None:
        raise UnrenderableContent(f"source journal {journal_path!r} is not <year>-<group>.journal; the export has no "
                                  f"file to put it in", path="journal_path", value=journal_path)
    group = match.group(1)
    if not _GROUP.match(group):
        raise UnrenderableContent(f"source group {group!r} is not a plain lowercase name", path="journal_path", value=journal_path)
    return group


def check_text(value: str, *, path: str, what: str) -> str:
    """Text that has to survive a journal round trip: no newline, no carriage return, no comment character."""
    if not isinstance(value, str) or not value:
        raise UnrenderableContent(f"{what} is required", path=path, value=value)
    for bad in _BAD_IN_TEXT:
        if bad in value:
            raise UnrenderableContent(f"{what} contains {bad!r}, which a journal line cannot carry", path=path, value=value)
    if value != value.strip():
        raise UnrenderableContent(f"{what} has leading or trailing whitespace, which a reparse would drop", path=path, value=value)
    return value


def check_account(path_: str) -> str:
    check_text(path_, path="account", what="account path")
    if "\t" in path_ or "  " in path_:
        raise UnrenderableContent("account path contains a tab or a double space, which ends the account name",
                                  path="account", value=path_)
    return path_


def render_symbol(code: str) -> str:
    """A commodity symbol as it is written in a journal.

    Only simple alphanumeric codes are emitted bare.  Anything else would need quoting rules we do not implement
    exactly, so it is refused rather than guessed."""
    check_text(code, path="commodity", what="commodity code")
    if not _SIMPLE_SYMBOL.match(code):
        raise UnrenderableContent(f"commodity symbol {code!r} is not a plain alphanumeric code", path="commodity", value=code)
    return code


def render_amount(amount: Micros, commodity: Commodity) -> str:
    """Integer micros -> the commodity's declared number of decimals, exactly (never a float, never a stored string)."""
    return amount.to_decimal_string(commodity)


def commodity_directive(commodity: Commodity) -> str:
    """``commodity USD 1000.00``: the declared precision, written as a style with no digit-group mark."""
    # hledger 1.52 refuses a directive with no decimal mark at all ("Please include a decimal point..."), so a
    # 0-decimal commodity is declared as `1000.`.  That is hledger's own "no decimals" example, and
    # `hledger.precision_of_style` reads it back as precision 0.
    sample = "1000." + "0" * commodity.precision
    return f"commodity {render_symbol(commodity.code)} {sample}"


def memo_lines(memo: str | None) -> list[str]:
    """The memo split for rendering: the first line goes on the posting line, the rest are continuation lines.

    A single trailing newline (the import keeps the comment verbatim) is not an extra empty line."""
    if memo is None or memo == "":
        return []
    if "\r" in memo:
        raise UnrenderableContent("memo contains a carriage return", path="memo", value=memo)
    body = memo[:-1] if memo.endswith("\n") else memo
    return body.split("\n")


def render_code(code: str) -> str:
    """``(code) `` as hledger writes a transaction code, right after the status mark; ``''`` when there is none."""
    if code == "":
        return ""
    check_text(code, path="code", what="transaction code")
    if "(" in code or ")" in code:
        raise UnrenderableContent("transaction code contains a parenthesis, which ends the code", path="code", value=code)
    return f"({code}) "


def comment_lines(txn: RTransaction) -> list[str]:
    """The transaction comment split for rendering: the first line goes on the transaction's own line, the rest
    above the first posting, which is how hledger reads them back into one ``tcomment``.

    A tag the comment does not itself contain is refused rather than dropped: hledger derives ``ttags`` by parsing
    the comment, so a tag with nowhere to be written is a tag this export cannot reproduce.  Whether the comment
    reproduces exactly these tags is not checked here, since that would mean re-implementing hledger's tag
    parser.  :mod:`haddon.oracle` proves it by reparsing the export with the pinned binary.
    """
    lines = memo_lines(txn.comment)
    for name, _ in txn.tags:
        if f"{name}:" not in txn.comment:
            raise UnrenderableContent(f"tag {name!r} is not written in the transaction comment, so a reparse of this "
                                      f"export could not produce it", path="tags", value=name)
    return lines


def transaction_header(txn: RTransaction) -> str:
    """The transaction's own line: date, status mark, code, narration and the first line of its comment."""
    check_text(txn.narration, path="narration", what="narration")
    if txn.mark not in ("", "*", "!"):
        raise UnrenderableContent(f"status mark {txn.mark!r} is not one of '', '*', '!'", path="mark", value=txn.mark)
    if not txn.mark and txn.narration[0] in "*!":
        raise UnrenderableContent(f"unmarked narration {txn.narration!r} starts with a status mark", path="narration",
                                  value=txn.narration)
    if txn.narration.startswith("("):
        raise UnrenderableContent(f"narration {txn.narration!r} starts with '(', which a reparse would read as the "
                                  f"transaction code", path="narration", value=txn.narration)
    head = f"{txn.dt} {txn.mark} " if txn.mark else f"{txn.dt} "
    head += render_code(txn.code) + txn.narration
    lines = comment_lines(txn)
    if lines and lines[0]:
        return f"{head}{GAP}; {lines[0]}"
    # An empty first line means "no comment on this line".  When nothing follows to imply it, it is still written
    # as a bare `;`, or a one-newline comment would come back as no comment at all.
    return f"{head}{GAP};" if len(lines) == 1 and txn.comment else head


def transaction_comment_lines(txn: RTransaction) -> list[str]:
    """The comment lines written between the transaction line and its first posting."""
    return [f"{INDENT};" if line == "" else f"{INDENT}; {line}" for line in comment_lines(txn)[1:]]


def _posting_lines(p: RPosting, commodities: dict[str, Commodity], *, account_width: int, amount_width: int) -> list[str]:
    commodity = commodities.get(p.commodity)
    if commodity is None:
        raise UnrenderableContent(f"posting commodity {p.commodity!r} is not declared in this book", path="commodity",
                                  value=p.commodity)
    text = f"{INDENT}{check_account(p.account):<{account_width}}{GAP}{render_symbol(p.commodity)} " \
           f"{render_amount(p.amount, commodity):>{amount_width}}"
    lines = memo_lines(p.memo)
    if not lines:
        return [text]
    return [f"{text}{GAP}; {lines[0]}".rstrip() if lines[0] == "" else f"{text}{GAP}; {lines[0]}"] + \
           [f"{MEMO_INDENT}; {line}".rstrip() if line == "" else f"{MEMO_INDENT}; {line}" for line in lines[1:]]


def _header(state: BookState, *lines: str) -> list[str]:
    check_text(state.entity_name, path="entity_name", what="entity name")
    return [f"; {state.entity_name} ({state.entity_id}) - {state.label}", *[f"; {line}" for line in lines], ""]


def _text(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def accounts_journal(state: BookState) -> str:
    """Every declared account of the chart, byte-ordered.  ``hledger check --strict`` needs exactly this file."""
    if not state.accounts:
        raise UnrenderableContent("the book declares no account", path="accounts")
    lines = _header(state, f"chart of accounts: {len(state.accounts)} declared, exported from the Haddon store")
    lines += [f"account {check_account(path)}" for path in state.accounts]
    return _text(lines)


def group_journal(state: BookState, group: str, transactions: tuple[RTransaction, ...]) -> str:
    """One source group's file, with account and amount columns aligned across the whole file."""
    commodities = {c.code: c for c in state.commodities}
    postings = [p for t in transactions for p in t.postings]
    account_width = max((len(check_account(p.account)) for p in postings), default=1)
    amount_width = max((len(render_amount(p.amount, commodities[p.commodity])) for p in postings
                        if p.commodity in commodities), default=1)
    lines = _header(state, f"{group}: {len(transactions)} transaction(s), {len(postings)} posting(s), "
                           f"exported from the Haddon store")
    for txn in transactions:
        lines.append(transaction_header(txn))
        lines += transaction_comment_lines(txn)
        for p in txn.postings:
            lines += _posting_lines(p, commodities, account_width=account_width, amount_width=amount_width)
        lines.append("")
    return _text(lines[:-1]) if lines and lines[-1] == "" else _text(lines)


def main_journal(state: BookState, group_files: list[str]) -> str:
    """The include file: the commodity directives, then ``accounts.journal``, the opening group, then the rest."""
    if not state.commodities:
        raise UnrenderableContent("the book declares no commodity", path="commodities")
    lines = _header(state, f"range {state.start}..{state.end}, exported from the Haddon store; do not edit by hand",
                    f"source_revision: {state.source_revision}", f"state_hash: {state_hash(state)}")
    lines += [commodity_directive(c) for c in state.commodities]
    lines += ["", "include accounts.journal"]
    lines += [f"include {name}" for name in group_files]
    return _text(lines)


def grouped(state: BookState) -> list[tuple[str, tuple[RTransaction, ...]]]:
    """Transactions bucketed by source group; groups byte-ordered with the opening group first."""
    buckets: dict[str, list[RTransaction]] = {}
    for txn in sorted(state.transactions, key=lambda t: t.sort_key):
        buckets.setdefault(txn.group, []).append(txn)
    names = sorted(buckets)
    if OPENING_GROUP in buckets:
        names = [OPENING_GROUP] + [n for n in names if n != OPENING_GROUP]
    return [(name, tuple(buckets[name])) for name in names]


def render_files(state: BookState) -> dict[str, str]:
    """The whole managed directory: ``<label>.journal``, ``accounts.journal`` and one ``<label>-<group>.journal``
    per source group present in the range.  Keys are byte-ordered so the artifact list is ordered too."""
    if not state.transactions:
        raise UnrenderableContent(f"range {state.start}..{state.end} has no posted entry to export", path="transactions")
    groups = grouped(state)
    files = {f"{state.label}-{group}.journal": group_journal(state, group, txns) for group, txns in groups}
    files["accounts.journal"] = accounts_journal(state)
    files[f"{state.label}.journal"] = main_journal(state, [f"{state.label}-{group}.journal" for group, _ in groups])
    return {name: files[name] for name in sorted(files)}


def state_document(state: BookState) -> dict:
    """The canonical record of exactly what is rendered.  Its sha256 is the snapshot's ``state_hash``: change any
    amount, account, narration, group or ordering input and the hash changes."""
    return {
        "schema": SCHEMA,
        "entity": {"id": state.entity_id, "name": state.entity_name, "dir": state.entity_dir},
        "range": {"start": state.start, "end": state.end, "source_revision": state.source_revision},
        "commodities": [{"code": c.code, "precision": c.precision} for c in state.commodities],
        "accounts": list(state.accounts),
        "transactions": [
            {"entry_id": t.entry_id, "dt": t.dt, "narration": t.narration, "mark": t.mark, "origin": t.origin,
             "group": t.group, "code": t.code, "comment": t.comment, "tags": [list(tag) for tag in t.tags],
             "postings": [{"account": p.account, "commodity": p.commodity, "amount_micros": p.amount.render(),
                           "memo": p.memo} for p in t.postings]}
            for t in sorted(state.transactions, key=lambda t: t.sort_key)],
    }


def state_hash(state: BookState) -> str:
    return sha256_hex(canonical_json(state_document(state)))
