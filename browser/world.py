"""The books the browser is pointed at: one entity, one month, one obviously invented bank statement.

Built fresh on every run through the real repository API (the migration, provisioning, ``load_statement`` and
``lines.book_line``/``ignore_line``), so the page is reading a database that got into this state the way a
real one does.  Nothing here writes to a database it was not handed, nothing survives the run, and every string
a person can see on screen is marked ``DEMO`` so a screenshot of a failing run can never be mistaken for a real
company's books.

The month is composed so that the review page has something to say in each of its registers:

* two lines booked, which puts an entry and a document behind a line for the evidence step;
* one line excluded with a reason: answered without being booked;
* two lines left needing an account, which the headline counts and which block the close.

And two things that are not statement lines at all, for the review inbox's other registers: one posted booking
awaiting a document, and the recoverable tax inside it that was not reclaimed.  Both are dated in the month
before the reviewed one on purpose (see ``AWAITED_DT``).

The statement's closing balance is its opening plus its own lines, so the statement is coherent on its own terms
and the difference the month page shows against the books is the three lines nothing was posted for.  The test
asserts those three figures to the cent: they are the reconciliation's arithmetic, its sign convention and its
formatting, all the way to the screen.

The chart has three cash accounts and this statement is for one of them, so the page also has two accounts it
cannot compare and says so.  That is deliberate: "no statement covers this month" is half of what a reviewer
needs to know, and a fixture where every account is covered would never exercise it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# ``provision_small_entity`` is the suite's one definition of the synthetic company, and a second one here would
# be a second thing to keep in step with the chart.  It lives in tests/, which is not a package.
sys.path.insert(0, str(REPO.joinpath("tests")))

from haddon.db.connection import make_engine                # noqa: E402
from haddon.db.migrate import upgrade                       # noqa: E402
from haddon.domain.micros import Micros                     # noqa: E402
from haddon.repository import entries as en                 # noqa: E402
from haddon.repository import expectations as ex            # noqa: E402
from haddon.repository import lines as ln                   # noqa: E402
from haddon.repository import statements as st              # noqa: E402
from haddon.repository import unrecovered as ur             # noqa: E402
from haddon.repository.ctx import WriteCtx                  # noqa: E402
from haddon.repository.transaction import write_transaction  # noqa: E402

from repo_helpers import CHART, provision_small_entity      # noqa: E402

BANK = "Ravensmoor Mutual"     # invented for this harness; the ledger names no institution
MARK = "DEMO"
ACTOR = "human:reviewer"
CASH = "assets:cash"
OFFICE = "expenses:office"
SALES = "income:sales"
MONTH = "2025-03"
START, END = f"{MONTH}-01", f"{MONTH}-31"

#: (day, micros, description, reference, what to do with it)
#: The two ``book`` lines are the ones the evidence step opens; the two ``leave`` lines are what the headline
#: counts and what the close refuses over.
ROWS = (
    (4, "-45000000", "bookkeeping monthly fee", "RM-0041", "book"),
    (9, "-12500000", "stationery and print", "RM-0042", "book"),
    (14, "-3300000", "account maintenance charge", "RM-0043", "exclude"),
    (18, "250000000", "inbound transfer - unidentified payer", "RM-0044", "leave"),
    (23, "-19900000", "card payment - no receipt yet", "RM-0045", "leave"),
)

#: what each booked line is booked to
CONTRA = {"RM-0041": OFFICE, "RM-0042": OFFICE}

EXCLUSION = f"{MARK}: a bank charge these books deliberately leave out of scope"
HEADER = ("date", "description", "reference", "amount_micros", "currency")

PAYABLES = "liabilities:payables"

#: One booking that is finished as far as the sums go and unfinished as far as the file cabinet goes, plus the
#: recoverable tax inside it that these books did not reclaim.  Both are registers of the review inbox that no
#: statement line can produce, and both are outside the reviewed month on purpose.  Dated in March they would
#: appear in the month page's attention list and change a count the reconciliation test asserts, coupling the
#: two tests through the fixture.
AWAITED_DT = "2025-02-18"
AWAITED_MICROS = 48900000            # 48.90 USD gross
AWAITED_NARRATION = f"{MARK} - consultancy, invoiced but the invoice has not arrived"
AWAITING = f"{MARK}: the supplier's invoice"
AWAITING_REASON = f"{MARK}: booked at the quoted figure until the invoice settles it"
TAX_MICROS = "9450000"               # 9.45 USD of the gross above
TAX_CODE = f"{MARK}-VAT"
TAX_REASON = f"{MARK}: no valid invoice, so this tax cannot be reclaimed"
TAX_UNTIL = "2025-12-31"


@dataclass(frozen=True)
class World:
    """Where the books are, and every string and figure the test looks for on screen.

    The figures are computed from ``ROWS`` rather than written out beside it, so changing a demo amount cannot
    leave the test asserting a number the fixture no longer produces.
    """

    db_path: Path
    #: where the statement's bytes are kept.  The harness configures this as the service's evidence root, so
    #: the browser can actually open the document rather than being shown a hash of it (issue 0007).
    blobs: Path
    entity_id: str
    month: str
    booked_reference: str        # the line the evidence step opens
    booked_description: str
    document_name: str
    document_sha256: str
    document_text: str           # the bytes themselves, so the test can check what the browser was handed
    statement_lines: int
    unexplained: int
    uncovered_accounts: int      # cash accounts with no statement, each of which the month page reports
    excluded_reason: str
    #: the booking that stands in the books while its document is still outstanding, and the tax inside it
    awaited_narration: str
    awaiting: str
    tax_code: str
    tax_total: str               # the unrecovered total, as the page prints it
    bank_total: str              # what the statement closes at, as the page prints it
    books_total: str             # what the cash account holds, ditto
    difference: str              # the magnitude between them; the page says which side is bigger separately


def _money(micros: int) -> str:
    """Micros as the page prints them: two decimals, thousands separated, sign kept.

    Integer arithmetic only, like the amount path itself (``test_no_float_reaches_db`` forbids true
    division in ``src/``).
    """
    sign = "-" if micros < 0 else ""
    units, hundredths = divmod(abs(micros) // 10000, 100)
    return f"{sign}{units:,}.{hundredths:02d}"


def _document_bytes(rows) -> str:
    """The statement as the bank would have handed it over, so the harness's provenance is a real file."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow((f"{MARK} - {BANK} - {CASH} - {MONTH}", "", "", "", ""))
    writer.writerow(HEADER)
    for day, micros, description, reference, _ in rows:
        writer.writerow((f"{MONTH}-{day:02d}", f"{MARK} - {description}", reference, micros, "USD"))
    return out.getvalue()


def build(db_path: str | Path, blob_dir: str | Path) -> World:
    """Migrate, provision, load the statement, book two lines and exclude one.  Returns what to look for."""
    path = Path(db_path)
    blobs = Path(blob_dir)
    blobs.mkdir(parents=True, exist_ok=True)
    upgrade(path)

    engine = make_engine(path)
    try:
        fx = provision_small_entity(engine)
        document_text = _document_bytes(ROWS)
        blob = blobs.joinpath(f"{MARK.lower()}-{BANK.lower().replace(' ', '-')}-{MONTH}.csv")
        blob.write_text(document_text, encoding="utf-8")
        digest = hashlib.sha256(document_text.encode("utf-8")).hexdigest()

        # The lines are read back out of the bytes just written, rather than built beside them: a fixture whose
        # provenance is asserted about a hash of a string proves nothing about provenance.
        parsed = list(csv.reader(io.StringIO(document_text)))[2:]
        lines = [st.Line(dt=row[0], description=row[1], reference=row[2], amount_micros=row[3], currency=row[4],
                         raw=",".join(row))
                 for row in parsed]
        total = sum(int(micros) for _, micros, *_ in ROWS)
        document = st.Document(sha256=digest, filename=blob.name, media_type="text/csv",
                               size_bytes=len(document_text.encode("utf-8")), blob_path=str(blob))

        with write_transaction(engine) as conn:
            ctx = WriteCtx.open(conn, ACTOR)
            statement = st.load_statement(
                ctx, account_id=fx.acct(CASH), commodity="USD", period_start=START, period_end=END,
                opening_micros=Micros(0), closing_micros=Micros(total), lines=lines, document=document, page=1,
                locator=f"{MARK}: an invented statement, not a document from any bank",
                reason=f"{MARK}: the month the reviewer workflow harness reviews")
            stored = st.statement_lines(conn, fx.entity_id, statement.id)
            for row, (_, micros, description, reference, action) in zip(stored, ROWS):
                if action == "book":
                    ln.book_line(ctx, line_id=row.id,
                                 legs=[en.Leg(fx.acct(CONTRA[reference]), "USD", -Micros(int(micros)))],
                                 narration=f"{MARK} - {description}",
                                 reason=f"{MARK}: what this line is for")
                elif action == "exclude":
                    ln.ignore_line(ctx, line_id=row.id, reason=EXCLUSION)

            # A posted entry outside the statement's month, so the inbox has a register the month cannot
            # produce.  Gross to payables, which keeps it off every cash account and so out of the
            # reconciliation the other test asserts to the cent.
            awaited = en.post_entry(
                ctx, dt=AWAITED_DT, narration=AWAITED_NARRATION, origin="invoice",
                legs=[en.Leg(fx.acct(OFFICE), "USD", Micros(AWAITED_MICROS)),
                      en.Leg(fx.acct(PAYABLES), "USD", -Micros(AWAITED_MICROS))])
            ex.expect_document(ctx, entry_id=awaited.id, awaiting=AWAITING, reason=AWAITING_REASON)
            ur.record_unrecovered(ctx, entry_id=awaited.id, commodity="USD", amount_micros=TAX_MICROS,
                                  reason=TAX_REASON, tax_code=TAX_CODE, recoverable_until=TAX_UNTIL)
    finally:
        engine.dispose()

    booked = next(r for r in ROWS if r[4] == "book")
    booked_total = sum(int(micros) for _, micros, *_, action in ROWS if action == "book")
    cash_accounts = sum(1 for _, _, is_cash, _ in CHART if is_cash)
    return World(db_path=path, blobs=blobs, entity_id=fx.entity_id, month=MONTH, booked_reference=booked[3],
                 booked_description=f"{MARK} - {booked[2]}", document_name=blob.name, document_sha256=digest,
                 document_text=document_text,
                 statement_lines=len(ROWS),
                 unexplained=sum(1 for r in ROWS if r[4] == "leave"),
                 # every cash account the chart has except the one this statement is for
                 uncovered_accounts=cash_accounts - 1,
                 excluded_reason=EXCLUSION,
                 awaited_narration=AWAITED_NARRATION, awaiting=AWAITING, tax_code=TAX_CODE,
                 tax_total=_money(int(TAX_MICROS)),
                 bank_total=_money(total), books_total=_money(booked_total),
                 difference=_money(abs(booked_total - total)))
