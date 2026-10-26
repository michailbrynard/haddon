"""The books the statement-replacement check is pointed at: two statements, in the two states that matter.

Separate from ``world.py`` because ``world.py``'s month ends closed (the reviewer-workflow test shuts March and
then April), and a replacement into a locked period is refused before anything else is looked at.  A replacement
check sharing those books would be asserting about a period lock rather than about replacement, and the order
two test files happen to run in would decide which.  So this builds its own entity, database and server.

Two statements, each in one of the states this screen exists to tell apart:

* The one that can be replaced: an EUR statement whose parse stopped at a page break, so it holds three of the
  document's four rows and closes at the subtotal printed on the first page.  Nothing is booked against it.
  This is the correction the repository calls the commonest: the same download, read properly.
* The one that cannot be, yet: a USD statement with a line booked.  Replacing it would leave the entry that
  line posted resting on a statement that is no longer there, so the service refuses, and the screen has to say
  which line and where to go.

The EUR document's bytes hold all four rows on purpose.  The file is not truncated; the reading of it was.
That makes "the same document, read again" the honest fix and gives the check a real predecessor chain to
assert about, rather than a second file that happens to be different.

Every string a person can see is marked ``DEMO`` and the bank is invented, so a screenshot of a failing run can
never be mistaken for a real company's books.
"""

from __future__ import annotations

import csv
import hashlib
import io
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO.joinpath("tests")))

from haddon.db.connection import make_engine                 # noqa: E402
from haddon.db.migrate import upgrade                        # noqa: E402
from haddon.domain.micros import Micros                      # noqa: E402
from haddon.repository import entries as en                  # noqa: E402
from haddon.repository import lines as ln                    # noqa: E402
from haddon.repository import statements as st               # noqa: E402
from haddon.repository.ctx import WriteCtx                   # noqa: E402
from haddon.repository.transaction import write_transaction   # noqa: E402

from repo_helpers import provision_small_entity              # noqa: E402

MARK = "DEMO"
BANK = "Ravensmoor Mutual"
ACTOR = "human:reviewer"
MONTH = "2025-06"
START, END = f"{MONTH}-01", f"{MONTH}-30"

EUR_ACCOUNT = "assets:cash-eur"
USD_ACCOUNT = "assets:cash"
OFFICE = "expenses:office"

#: The EUR document, all four rows of it.  The loaded statement below stops at the third.
EUR_ROWS = (
    ("2025-06-03", "-24000000", "bookkeeping monthly fee", "RE-0091"),
    ("2025-06-11", "-8500000", "stationery and print", "RE-0092"),
    ("2025-06-19", "-13750000", "courier, quarterly", "RE-0093"),
    ("2025-06-27", "-41250000", "workshop rent, june", "RE-0094"),
)
#: how many of those rows the parse actually got, and what the first page's subtotal therefore said
EUR_READ = 3

#: The USD document.  Its first line is booked, which is what holds its statement in place.
USD_ROWS = (
    ("2025-06-05", "-66000000", "translation of the annual accounts", "RU-0071"),
    ("2025-06-14", "-15400000", "card payment - no receipt yet", "RU-0072"),
    ("2025-06-22", "92000000", "inbound transfer - unidentified payer", "RU-0073"),
)
BOOKED_REFERENCE = "RU-0071"

HEADER = ("date", "description", "reference", "amount_micros", "currency")


@dataclass(frozen=True)
class Statements:
    """Where the books are, and every id, string and figure the check looks for on screen.

    The figures are computed from the rows above rather than written out beside them, so changing a demo amount
    cannot leave the check asserting a number the fixture no longer produces.
    """

    db_path: Path
    blobs: Path
    entity_id: str

    #: the replaceable one: three of four rows, EUR
    eur_id: str
    eur_document: str
    eur_document_text: str
    eur_closing: str          # what it was loaded closing at, as the form prints it (a decimal, not grouped)
    eur_true_closing: str     # what it should close at once the fourth row is read
    eur_missing: tuple[str, str, str, str]   # the row the parse never reached
    eur_lines_read: int

    #: the blocked one: a USD statement with a booked line
    usd_id: str
    usd_document: str
    usd_document_text: str
    usd_booked_description: str
    usd_booked_entry_id: str
    usd_lines: int


def _decimal(micros: int) -> str:
    """Micros as the replacement form writes an amount: exact, ungrouped, two decimals, sign kept.

    Integer arithmetic only, for the same reason as ``world.py``'s formatter: the amount path is integer-only.
    """
    sign = "-" if micros < 0 else ""
    units, hundredths = divmod(abs(micros) // 10000, 100)
    return f"{sign}{units}.{hundredths:02d}"


def _document(rows, account: str, commodity: str) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow((f"{MARK} - {BANK} - {account} - {MONTH}", "", "", "", ""))
    writer.writerow(HEADER)
    for dt, micros, description, reference in rows:
        writer.writerow((dt, f"{MARK} - {description}", reference, micros, commodity))
    return out.getvalue()


def _lines(rows, commodity: str) -> list[st.Line]:
    return [st.Line(dt=dt, description=f"{MARK} - {description}", amount_micros=micros, currency=commodity,
                    reference=reference, raw=f"{dt},{MARK} - {description},{reference},{micros},{commodity}")
            for dt, micros, description, reference in rows]


def build(db_path: str | Path, blob_dir: str | Path) -> Statements:
    path, blobs = Path(db_path), Path(blob_dir)
    blobs.mkdir(parents=True, exist_ok=True)
    upgrade(path)

    engine = make_engine(path)
    try:
        fx = provision_small_entity(engine)
        eur_text = _document(EUR_ROWS, EUR_ACCOUNT, "EUR")
        usd_text = _document(USD_ROWS, USD_ACCOUNT, "USD")
        eur_blob = blobs.joinpath(f"{MARK.lower()}-ravensmoor-eur-{MONTH}.csv")
        usd_blob = blobs.joinpath(f"{MARK.lower()}-ravensmoor-usd-{MONTH}.csv")
        eur_blob.write_text(eur_text, encoding="utf-8")
        usd_blob.write_text(usd_text, encoding="utf-8")

        read = EUR_ROWS[:EUR_READ]
        eur_partial = sum(int(micros) for _, micros, *_ in read)
        eur_whole = sum(int(micros) for _, micros, *_ in EUR_ROWS)
        usd_total = sum(int(micros) for _, micros, *_ in USD_ROWS)

        with write_transaction(engine) as conn:
            ctx = WriteCtx.open(conn, ACTOR)
            eur = st.load_statement(
                ctx, account_id=fx.acct(EUR_ACCOUNT), commodity="EUR", period_start=START, period_end=END,
                opening_micros=Micros(0), closing_micros=Micros(eur_partial), lines=_lines(read, "EUR"),
                document=st.Document(sha256=hashlib.sha256(eur_text.encode("utf-8")).hexdigest(),
                                     filename=eur_blob.name, media_type="text/csv",
                                     size_bytes=len(eur_text.encode("utf-8")), blob_path=str(eur_blob)),
                page=1, locator=f"{MARK}: the subtotal at the foot of page 1",
                reason=f"{MARK}: the june euro statement, as it was first read")
            usd = st.load_statement(
                ctx, account_id=fx.acct(USD_ACCOUNT), commodity="USD", period_start=START, period_end=END,
                opening_micros=Micros(0), closing_micros=Micros(usd_total), lines=_lines(USD_ROWS, "USD"),
                document=st.Document(sha256=hashlib.sha256(usd_text.encode("utf-8")).hexdigest(),
                                     filename=usd_blob.name, media_type="text/csv",
                                     size_bytes=len(usd_text.encode("utf-8")), blob_path=str(usd_blob)),
                page=1, locator=f"{MARK}: the closing balance printed on page 1",
                reason=f"{MARK}: the june dollar statement")

            stored = st.statement_lines(conn, fx.entity_id, usd.id)
            booked_row = next(row for row in stored if row.reference == BOOKED_REFERENCE)
            explained = ln.book_line(
                ctx, line_id=booked_row.id,
                legs=[en.Leg(fx.acct(OFFICE), "USD", -Micros(int(booked_row.amount_micros)))],
                narration=f"{MARK} - translation of the annual accounts",
                reason=f"{MARK}: what this line is for")
    finally:
        engine.dispose()

    missing = EUR_ROWS[EUR_READ]
    return Statements(
        db_path=path, blobs=blobs, entity_id=fx.entity_id,
        eur_id=eur.id, eur_document=eur_blob.name, eur_document_text=eur_text,
        eur_closing=_decimal(eur_partial), eur_true_closing=_decimal(eur_whole),
        eur_missing=(missing[0], f"{MARK} - {missing[2]}", missing[3], _decimal(int(missing[1]))),
        eur_lines_read=EUR_READ,
        usd_id=usd.id, usd_document=usd_blob.name, usd_document_text=usd_text,
        usd_booked_description=f"{MARK} - translation of the annual accounts",
        usd_booked_entry_id=explained.entry.id, usd_lines=len(USD_ROWS))
