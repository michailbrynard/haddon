"""Write the fictional sample corpus in ``corpus/example/`` (the corpus ``corpus/example.json`` names).

Every company, supplier, customer, amount and date in it is invented.  It exists so the importer, the exporter
and the oracle can be verified end to end, at a realistic scale, across year boundaries and through opening
restatements, without anybody's real books.

The output is deterministic (one seeded generator), so re-running this script reproduces the checked-in files
byte for byte.  Do not regenerate to make a failing run pass.  The oracle's pinned figures in
``corpus/example-baselines.json`` are measured from these files; a change here is a change to the source, and
the baselines move with it deliberately (see ``corpus/README.md``).

    python corpus/generate_example.py            # rewrite corpus/example/
    python corpus/generate_example.py --check    # exit 1 if the checked-in files differ from what it writes

The layout is the one the importer expects of any corpus: ``<Entity>/<year>/ledger/<year>.journal`` including an
``accounts.journal`` and one ``<year>-<group>.journal`` per source.  Each later year opens with a
``<year>-opening.journal`` that restates the previous year's closing balance sheet, with the year's result folded
into retained earnings.  That makes each year a self-contained annual book, the shape the continuity oracle
verifies.

What the three books are built to exercise:

* Example Inc. (USD), the scale book: well over a thousand transactions, eleven source files that carry
  transactions and two that are declared but empty, posting comments, transaction comments and tags, an
  account tag a posting inherits without having a comment of its own, and a float-sensitive amount (-512.17).
* Example B.V. (EUR), a small book whose 2026 holds nothing but its opening restatement, and whose 2026
  chart declares accounts 2025 did not.
* Example S.L. (EUR), a company constituted in August 2024, so its chain is 2024 -> 2025 -> 2026; its
  2024 year-end is asserted with zero postings carrying a balance assertion.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "example"
CENT = Decimal("0.01")
SAMPLE_NOTE = "; Fictional sample data: every name, amount and date in this file is invented."


@dataclass
class Posting:
    account: str
    amount: Decimal
    comment: str = ""          # posting comment, written after the amount
    assertion: Decimal | None = None
    zero_style: bool = False   # write as `EUR 0 = EUR x` (the year-end assertion idiom)


@dataclass
class Txn:
    date: str
    description: str
    postings: list[Posting]
    comment: str = ""              # same-line transaction comment (may carry tags)
    comment_lines: list[str] = field(default_factory=list)  # comment lines under the header


@dataclass
class Book:
    """One entity-year: its chart, its source files in include order, and the lines each file carries."""

    entity: str
    year: int
    currency: str
    title: str
    header: list[str]              # comment lines at the top of <year>.journal
    accounts: list[tuple[str, str]]   # (path, comment)
    files: dict[str, list[Txn]]    # group -> transactions, in include order
    file_notes: dict[str, str] = field(default_factory=dict)
    commodity_in_main: bool = True
    commodity_in_accounts: bool = False


def money(value) -> Decimal:
    return Decimal(value).quantize(CENT)


def fmt(amount: Decimal) -> str:
    return f"{amount:.2f}"


def balances(books: list[Book]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(Decimal)
    for book in books:
        for txns in book.files.values():
            for t in txns:
                for p in t.postings:
                    out[p.account] += p.amount
    return out


def opening_from(previous: list[Book], *, date: str, retained: str, description: str) -> Txn:
    """The next year's opening: the previous year's closing balance sheet, with the result folded into
    ``retained``.  Balance-sheet accounts only; zero balances are left out."""
    held = balances(previous)
    result = sum((v for a, v in held.items() if a.split(":")[0] in ("income", "expenses")), Decimal())
    lines: dict[str, Decimal] = {a: v for a, v in held.items() if a.split(":")[0] in ("assets", "liabilities", "equity")}
    lines[retained] = lines.get(retained, Decimal()) + result
    order = {"assets": 0, "liabilities": 1, "equity": 2}
    postings = [Posting(a, v) for a, v in sorted(lines.items(), key=lambda kv: (order[kv[0].split(":")[0]], kv[0]))
                if v != 0]
    assert sum(p.amount for p in postings) == 0
    return Txn(date, description, postings)


def render_txn(t: Txn, currency: str) -> list[str]:
    head = f"{t.date} * {t.description}"
    if t.comment:
        head += f"  ; {t.comment}"
    out = [head]
    out += [f"    ; {line}" if line else "    ;" for line in t.comment_lines]
    width = max(len(p.account) for p in t.postings) + 4
    for p in t.postings:
        if p.zero_style:
            body = f"{currency} 0 = {currency} {fmt(p.assertion)}"
        else:
            body = f"{fmt(p.amount):>12} {currency}"
            if p.assertion is not None:
                body += f" = {fmt(p.assertion)} {currency}"
        line = f"    {p.account.ljust(width)}{body}"
        if p.comment:
            line += f"  ; {p.comment}"
        out.append(line)
    return out


def write_book(book: Book, root: Path) -> dict[Path, str]:
    ledger = root / book.entity / str(book.year) / "ledger"
    files: dict[Path, str] = {}
    y = book.year
    main = list(book.header)
    main += [f"; Amounts written without a commodity are {book.currency}", f"D 1000.00 {book.currency}"]
    if book.commodity_in_main:
        main.append(f"commodity {book.currency} 1,000.00")
    main.append("")
    main.append("include accounts.journal")
    main += [f"include {y}-{group}.journal" for group in book.files]
    files[ledger / f"{y}.journal"] = "\n".join(main) + "\n"

    acc = [f"; {book.title} - chart of accounts, {y}", SAMPLE_NOTE, ""]
    if book.commodity_in_accounts:
        acc += [f"commodity {book.currency} 1000.00", ""]
    for path, comment in book.accounts:
        acc.append(f"account {path}")
        if comment:
            acc.append(f"    ; {comment}")
    files[ledger / "accounts.journal"] = "\n".join(acc) + "\n"

    for group, txns in book.files.items():
        lines = [f"; {book.title} - {group}, {y}", SAMPLE_NOTE]
        if book.file_notes.get(group):
            lines.append(f"; {book.file_notes[group]}")
        lines.append("")
        for t in txns:
            lines += render_txn(t, book.currency)
            lines.append("")
        files[ledger / f"{y}-{group}.journal"] = "\n".join(lines).rstrip("\n") + "\n"
    return files


MONTH_DAYS = 28  # every generated date is on or before the 28th, so the 29th..31st stay free for year-end entries


class Gen:
    def __init__(self, seed: int):
        self.r = random.Random(seed)

    def date(self, year: int, month: int, lo: int = 1, hi: int = MONTH_DAYS) -> str:
        return f"{year}-{month:02d}-{self.r.randint(lo, hi):02d}"

    def amount(self, lo: float, hi: float) -> Decimal:
        return money(self.r.uniform(lo, hi))

    def pick(self, seq):
        return self.r.choice(seq)


def by_date(txns: list[Txn]) -> list[Txn]:
    return sorted(txns, key=lambda t: t.date)


def pair(date: str, description: str, debit: str, credit: str, amount: Decimal, **kw) -> Txn:
    return Txn(date, description, [Posting(debit, amount), Posting(credit, -amount)], **kw)


# Example Inc.
INC_TITLE = "Example Inc."
INC_SUPPLIERS = {
    "expenses:software": ["Cloudberry Hosting", "Notably Docs", "Pixelforge Design", "Quillwork Mail", "Stackyard CI"],
    "expenses:office": ["Paperworks Supply", "Northdesk Furniture", "Brightline Print"],
    "expenses:travel": ["Skyward Air", "Railway Express", "Harbor Hotel Group", "Metro Cabs"],
    "expenses:meals": ["Corner Bistro", "Green Fork Catering", "Daily Grind Coffee"],
    "expenses:marketing": ["Signal Ads", "Bluebird Events", "Lantern PR"],
    "expenses:professional-fees": ["Oakridge Legal LLP", "Ledgerline Accounting"],
    "expenses:telecom": ["Wavelink Mobile", "Fiberhome ISP"],
    "expenses:insurance": ["Harborline Mutual"],
    "expenses:training": ["Skillforge Academy", "Open Course Co"],
    "expenses:hardware": ["Circuit Depot", "Keystone Peripherals"],
    "expenses:recruiting": ["Talentbridge", "Hireloop Boards"],
    "expenses:events": ["Summit Hall", "Meetup Space Co"],
    "expenses:postage": ["Parcelpoint", "Swift Couriers"],
    "expenses:utilities": ["Gridline Power", "Clearwater Utilities"],
    "expenses:cleaning": ["Sparkle Office Care"],
    "expenses:cloud-compute": ["Nimbus Compute", "Cirrus Storage"],
    "expenses:data": ["Datawell Feeds"],
    "expenses:security": ["Sentinel Audit Co", "Lockbox Security"],
    "expenses:design": ["Inkwell Studio"],
    "expenses:books-and-media": ["Readmore Books", "Newsstand Digital"],
}
INC_CUSTOMERS = ["Acorn Retail", "Bramble Logistics", "Cobalt Health", "Driftwood Media", "Ember Foods",
                 "Fjord Analytics", "Granite Insurance", "Hollow Pine Games"]

INC_ACCOUNTS = [
    ("assets:cash:first-bank", "operating account"),
    ("assets:cash:second-bank", "reserve account"),
    ("assets:cash:fx-account", "multi-currency account, held in USD"),
    ("assets:cash:payments-clearing", "balance held by the card processor"),
    ("assets:cash:custody", "digital-asset custody, carried at fair value in USD"),
    ("assets:cash:old-bank", "closed during 2024; kept for history"),
    ("assets:receivables:trade", ""),
    ("assets:prepaid", ""),
    ("assets:fixed:equipment", ""),
    ("assets:fixed:accumulated-depreciation", "contra-asset"),
    ("liabilities:credit-card", "corporate card"),
    ("liabilities:payables:trade", ""),
    ("liabilities:accrued", ""),
    ("liabilities:payroll-taxes", ""),
    ("liabilities:income-tax-payable", ""),
    ("equity:common-stock", ""),
    ("equity:additional-paid-in-capital", ""),
    ("equity:retained-earnings", ""),
    ("income:subscriptions", ""),
    ("income:services", ""),
    ("income:grants", ""),
    ("income:interest", ""),
    ("income:fx-gains", ""),
    ("expenses:salaries", ""),
    ("expenses:payroll-taxes", ""),
    ("expenses:contractors", ""),
    ("expenses:software", ""),
    ("expenses:office", ""),
    ("expenses:travel", ""),
    ("expenses:meals", ""),
    ("expenses:marketing", ""),
    ("expenses:professional-fees", ""),
    ("expenses:telecom", ""),
    ("expenses:bank-fees", ""),
    ("expenses:processing-fees", ""),
    ("expenses:depreciation", "booked at year end (class: depreciation of fixed assets)"),
    ("expenses:income-tax", ""),
    ("expenses:fx-losses", ""),
    ("expenses:insurance", ""),
    ("expenses:training", ""),
    ("expenses:hardware", ""),
    ("expenses:recruiting", ""),
    ("expenses:events", ""),
    ("expenses:postage", ""),
    ("expenses:utilities", ""),
    ("expenses:cleaning", ""),
    ("expenses:cloud-compute", ""),
    ("expenses:data", ""),
    ("expenses:security", ""),
    ("expenses:design", ""),
    ("expenses:books-and-media", ""),
    ("expenses:charitable-giving", "declared, never posted to"),
    ("expenses:unused-legacy", "declared, never posted to"),
    ("income:licensing", "declared, never posted to"),
]

INC_FILES = ["opening", "first-bank", "second-bank", "card", "fx-account", "payments", "payroll", "grants",
             "custody", "adjustments", "reclass", "tax-adjustments"]
EMPTY_NOTE = "Nothing was booked from this source this year; the file is kept so the include list stays stable."


def inc_2025(g: Gen) -> Book:
    y = 2025
    files: dict[str, list[Txn]] = {name: [] for name in INC_FILES}
    files["opening"] = [Txn(f"{y}-01-01", "Opening balances (per the audited 2024 balance sheet)", [
        Posting("assets:cash:first-bank", money("184250.40"), assertion=money("184250.40")),
        Posting("assets:cash:second-bank", money("60000.00")),
        Posting("assets:cash:fx-account", money("12480.15")),
        Posting("assets:cash:payments-clearing", money("8312.66")),
        Posting("assets:cash:custody", money("4100.00")),
        Posting("assets:receivables:trade", money("23900.00")),
        Posting("assets:fixed:equipment", money("31200.00")),
        Posting("assets:fixed:accumulated-depreciation", money("-9360.00")),
        Posting("liabilities:credit-card", money("-6120.88")),
        Posting("liabilities:payables:trade", money("-4380.50")),
        Posting("liabilities:accrued", money("-2500.00")),
        Posting("equity:common-stock", money("-1000.00")),
        Posting("equity:additional-paid-in-capital", money("-250000.00")),
        Posting("equity:retained-earnings", money("-50881.83")),
    ], comment_lines=["", "--- Cash ---", ""])]

    # the card: many small charges, paid off monthly from the first bank
    for m in range(1, 13):
        charged = Decimal()
        for _ in range(g.r.randint(44, 52)):
            account = g.pick(list(INC_SUPPLIERS))
            vendor = g.pick(INC_SUPPLIERS[account])
            amt = g.amount(4, 900)
            charged += amt
            t = pair(g.date(y, m), vendor, account, "liabilities:credit-card", amt)
            if g.r.random() < 0.06:
                eur = money(amt * Decimal("0.92"))
                t.postings[0].comment = f"orig: {fmt(eur)} EUR"
            files["card"].append(t)
        files["first-bank"].append(pair(f"{y}-{m:02d}-28", "Corporate card statement payment",
                                        "liabilities:credit-card", "assets:cash:first-bank", charged))
    files["card"] = by_date(files["card"])

    # the processor: subscription charges, its fees, and a payout to the bank every week or so
    for m in range(1, 13):
        gross_month = Decimal()
        for _ in range(g.r.randint(34, 40)):
            customer = g.pick(INC_CUSTOMERS)
            amt = money(g.pick([49, 99, 199, 499, 999]) * g.r.randint(1, 3))
            fee = money(amt * Decimal("0.029") + Decimal("0.30"))
            files["payments"].append(Txn(g.date(y, m), f"Subscription charge - {customer}", [
                Posting("assets:cash:payments-clearing", amt - fee),
                Posting("expenses:processing-fees", fee),
                Posting("income:subscriptions", -amt)]))
            gross_month += amt - fee
        for week, day in enumerate((7, 14, 21)):
            payout = money(gross_month * Decimal("0.3"))
            files["payments"].append(pair(f"{y}-{m:02d}-{day:02d}", f"Payout to first bank (week {week + 1})",
                                          "assets:cash:first-bank", "assets:cash:payments-clearing", payout))
    files["payments"] = by_date(files["payments"])

    # the first bank: customer invoices paid, supplier invoices paid, fees, interest
    for m in range(1, 13):
        for _ in range(g.r.randint(5, 7)):
            customer = g.pick(INC_CUSTOMERS)
            files["first-bank"].append(pair(g.date(y, m, 2, 27), f"Invoice payment - {customer}",
                                            "assets:cash:first-bank", "income:services", g.amount(1500, 18000),
                                            comment=f"invoice: INV-{y}{m:02d}{g.r.randint(10, 99)}"))
        files["first-bank"].append(pair(g.date(y, m, 2, 27), "Monthly account fee", "expenses:bank-fees",
                                        "assets:cash:first-bank", money("25.00")))
        files["first-bank"].append(pair(g.date(y, m, 2, 27), "Legal and accounting retainer",
                                        "expenses:professional-fees", "assets:cash:first-bank", g.amount(800, 2400)))
    files["first-bank"].append(pair(f"{y}-03-12", "Refund - duplicate supplier charge", "assets:cash:first-bank",
                                    "expenses:office", money("512.17")))
    files["first-bank"].append(pair(f"{y}-03-19", "Refund reversed by the supplier", "expenses:office",
                                    "assets:cash:first-bank", money("512.17")))
    files["first-bank"] = by_date(files["first-bank"])

    # the second bank: interest and the odd transfer
    for m in range(1, 13):
        files["second-bank"].append(pair(g.date(y, m, 2, 27), "Interest credited", "assets:cash:second-bank",
                                         "income:interest", g.amount(60, 140)))
        if m % 3 == 0:
            files["second-bank"].append(pair(g.date(y, m, 2, 27), "Transfer to the operating account",
                                             "assets:cash:first-bank", "assets:cash:second-bank", money("15000.00"),
                                             comment="transfer: reserve"))
    files["second-bank"] = by_date(files["second-bank"])

    # the multi-currency account: overseas contractors, converted at the bank's rate
    for m in range(1, 13):
        for _ in range(g.r.randint(2, 4)):
            amt = g.amount(900, 4200)
            eur = money(amt * Decimal("0.92"))
            t = pair(g.date(y, m, 2, 27), f"Contractor invoice - {g.pick(['Lumen Studio', 'Arcadia Dev', 'Northfield QA'])}",
                     "expenses:contractors", "assets:cash:fx-account", amt)
            t.postings[0].comment = f"orig: {fmt(eur)} EUR"
            files["fx-account"].append(t)
        files["fx-account"].append(pair(g.date(y, m, 2, 27), "Top-up from the operating account",
                                        "assets:cash:fx-account", "assets:cash:first-bank", money("9000.00")))
    files["fx-account"] = by_date(files["fx-account"])

    # payroll: twice a month, net pay and taxes
    for m in range(1, 13):
        for day in (14, 28):
            gross = g.amount(41000, 43000)
            tax = money(gross * Decimal("0.0765"))
            withheld = money(gross * Decimal("0.22"))
            files["payroll"].append(Txn(f"{y}-{m:02d}-{day:02d}", "Payroll run", [
                Posting("expenses:salaries", gross),
                Posting("expenses:payroll-taxes", tax),
                Posting("liabilities:payroll-taxes", -(tax * 2 + withheld)),
                Posting("assets:cash:first-bank", -(gross - tax - withheld))], comment="payroll: semi-monthly"))
            files["payroll"].append(pair(f"{y}-{m:02d}-{day:02d}", "Payroll taxes remitted",
                                         "liabilities:payroll-taxes", "assets:cash:first-bank", tax * 2 + withheld))
    files["payroll"] = by_date(files["payroll"])

    files["grants"] = [Txn(f"{y}-06-18", "Research grant received", [
        Posting("assets:cash:first-bank", money("25000.00"), assertion=None),
        Posting("income:grants", money("-25000.00"))], comment="grant: innovation-fund")]

    # custody: small digital-asset movements, carried in USD
    for m in (1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12):
        files["custody"].append(pair(g.date(y, m, 2, 27), "Custody fee", "expenses:bank-fees", "assets:cash:custody",
                                     g.amount(3, 12)))
    files["custody"].append(pair(f"{y}-04-10", "Fair value movement", "assets:cash:custody", "income:fx-gains",
                                 money("312.40"), comment="valuation: month-end"))
    files["custody"] = by_date(files["custody"])

    # adjustments: accruals, prepaid, receivable movements, and a year-end bank fee asserted against the balance
    for m in range(1, 13):
        files["adjustments"].append(pair(f"{y}-{m:02d}-28", "Month-end accrual", "expenses:professional-fees",
                                         "liabilities:accrued", money("500.00")))
    files["adjustments"].append(pair(f"{y}-02-10", "Annual insurance prepaid", "assets:prepaid",
                                     "assets:cash:first-bank", money("6000.00")))
    files["adjustments"].append(pair(f"{y}-11-20", "Customer invoice raised, unpaid at year end",
                                     "assets:receivables:trade", "income:services", money("12750.00"),
                                     comment="invoice: INV-2025-1120"))
    files["adjustments"].append(pair(f"{y}-12-29", "Accruals settled", "liabilities:accrued",
                                     "assets:cash:first-bank", money("8500.00")))
    files["adjustments"].append(pair(f"{y}-12-29", "Insurance expensed for the year", "expenses:office",
                                     "assets:prepaid", money("6000.00")))
    files["adjustments"].append(pair(f"{y}-12-29", "Income tax provision", "expenses:income-tax",
                                     "liabilities:income-tax-payable", money("14200.00")))
    files["adjustments"].append(pair(f"{y}-12-29", "Opening receivables collected", "assets:cash:first-bank",
                                     "assets:receivables:trade", money("23900.00")))
    files["adjustments"].append(pair(f"{y}-12-29", "Supplier balance paid", "liabilities:payables:trade",
                                     "assets:cash:first-bank", money("4380.50")))
    files["adjustments"] = by_date(files["adjustments"])

    # reclass: year-end depreciation, and two reclassifications
    files["reclass"] = [
        Txn(f"{y}-12-30", "Depreciation for the year", [Posting("expenses:depreciation", money("6240.00")),
                                                        Posting("assets:fixed:accumulated-depreciation", money("-6240.00"), comment="straight line, five years")]),
        pair(f"{y}-12-30", "Reclassify conference costs from travel to marketing", "expenses:marketing",
             "expenses:travel", money("1850.00"), comment="reclass: year-end"),
        pair(f"{y}-12-30", "Reclassify software subscription from office", "expenses:software", "expenses:office",
             money("420.00"), comment="reclass: year-end"),
        pair(f"{y}-12-30", "New laptops capitalised", "assets:fixed:equipment", "liabilities:credit-card",
             money("7800.00")),
        pair(f"{y}-12-30", "Card balance for the laptops settled", "liabilities:credit-card",
             "assets:cash:first-bank", money("7800.00")),
    ]

    # year-end assertions on the last transaction that touches each account, which nothing later-dated follows
    book = Book("ExampleInc", y, "USD", INC_TITLE, _main_header(INC_TITLE, y, "USD", 7), INC_ACCOUNTS, files,
                file_notes={"tax-adjustments": EMPTY_NOTE})
    _assert_last(book, "reclass", "assets:fixed:accumulated-depreciation")
    _assert_last(book, "reclass", "assets:cash:first-bank")
    return book


def inc_2026(previous: Book) -> Book:
    y = 2026
    opening = opening_from([previous], date=f"{y}-01-01", retained="equity:retained-earnings",
                           description="Opening balances (restating the 2025 closing balance sheet)")
    for p in opening.postings:
        if p.account.startswith(("assets:cash:", "liabilities:")):
            p.assertion = p.amount
    files = {name: [] for name in ("opening", "first-bank", "second-bank", "card", "fx-account", "payments",
                                   "payroll", "custody", "adjustments", "tax-adjustments", "assertions")}
    files["opening"] = [opening]
    accounts = INC_ACCOUNTS + [("expenses:research", "new in 2026")]
    notes = {name: "Nothing booked yet for this year." for name in files if name != "opening"}
    book = Book("ExampleInc", y, "USD", INC_TITLE, _main_header(INC_TITLE, y, "USD", 7), accounts, files,
                file_notes=notes, commodity_in_main=False, commodity_in_accounts=True)
    return book


# Example B.V.
BV_TITLE = "Example B.V."
BV_ACCOUNTS = [
    ("assets:cash:bank", "main current account"),
    ("assets:cash:savings", ""),
    ("assets:cash:custody", "digital assets, carried at fair value in EUR"),
    ("assets:receivables:intercompany", "due from Example Inc."),
    ("assets:receivables:vat", ""),
    ("liabilities:payables:trade", ""),
    ("liabilities:corporate-tax-payable", ""),
    ("liabilities:vat-payable", ""),
    ("equity:share-capital", ""),
    ("equity:retained-earnings", ""),
    ("income:intercompany-services", ""),
    ("income:interest", ""),
    ("income:crypto-gains", ""),
    ("expenses:rent", ""),
    ("expenses:professional-fees", ""),
    ("expenses:bank-fees", ""),
    ("expenses:software", ""),
    ("expenses:corporate-tax", ""),
    ("expenses:office", ""),
]


def bv_2025(g: Gen) -> Book:
    y = 2025
    files: dict[str, list[Txn]] = {name: [] for name in ("opening", "bank", "savings", "custody", "adjustments")}
    files["opening"] = [Txn(f"{y}-01-01", "Opening balances (per the 2024 annual accounts)", [
        Posting("assets:cash:bank", money("41230.55")),
        Posting("assets:cash:savings", money("20000.00")),
        Posting("assets:cash:custody", money("2875.00")),
        Posting("assets:receivables:intercompany", money("12000.00")),
        Posting("liabilities:payables:trade", money("-1840.00")),
        Posting("liabilities:corporate-tax-payable", money("-3105.20")),
        Posting("equity:share-capital", money("-18000.00")),
        Posting("equity:retained-earnings", money("-53160.35")),
    ])]
    for m in range(1, 13):
        files["bank"].append(pair(g.date(y, m, 2, 27), "Intercompany service fee - Example Inc.",
                                  "assets:cash:bank", "income:intercompany-services", g.amount(9000, 10000)))
        files["bank"].append(pair(g.date(y, m, 2, 27), "Office rent - Canal Street Offices", "expenses:rent",
                                  "assets:cash:bank", g.amount(2100, 2200)))
        for half in ((2, 14), (15, 27)):
            files["bank"].append(pair(g.date(y, m, *half), "Payment terminal charges", "expenses:bank-fees",
                                      "assets:cash:bank", g.amount(3, 30)))
        if m % 2 == 0:
            files["bank"].append(pair(g.date(y, m, 2, 27), "Bookkeeping - Ledgerline Accounting",
                                      "expenses:professional-fees", "assets:cash:bank", g.amount(400, 900)))
    files["bank"].append(pair(f"{y}-05-14", "Card terminal fee", "expenses:bank-fees", "assets:cash:bank",
                              money("17.35")))
    files["bank"].append(pair(f"{y}-06-03", "Corporate tax 2024 settled", "liabilities:corporate-tax-payable",
                              "assets:cash:bank", money("3105.20")))
    files["bank"].append(pair(f"{y}-07-16", "Supplier balance paid", "liabilities:payables:trade",
                              "assets:cash:bank", money("1840.00")))
    files["bank"] = by_date(files["bank"])
    for m in (3, 6, 9, 12):
        files["savings"].append(pair(g.date(y, m, 2, 27), "Interest credited", "assets:cash:savings",
                                     "income:interest", g.amount(40, 90)))
    files["savings"].append(pair(f"{y}-08-08", "Software licences", "expenses:software", "assets:cash:savings",
                                 money("1188.00"), comment="licence: annual"))
    files["savings"].append(pair(f"{y}-10-21", "Office equipment", "expenses:office", "assets:cash:savings",
                                 money("642.30")))
    files["savings"].append(pair(f"{y}-12-31", "Year-end interest credited", "assets:cash:savings",
                                 "income:interest", money("18.62")))
    files["savings"] = by_date(files["savings"])
    files["custody"] = [
        pair(f"{y}-02-11", "Disposal of digital assets at fair value", "assets:cash:bank", "assets:cash:custody",
             money("1200.00")),
        pair(f"{y}-09-09", "Fair value gain", "assets:cash:custody", "income:crypto-gains", money("415.60")),
        pair(f"{y}-12-29", "Custody fee", "expenses:bank-fees", "assets:cash:custody", money("35.00")),
    ]
    files["adjustments"] = [
        pair(f"{y}-12-29", "Intercompany balance settled", "assets:cash:bank", "assets:receivables:intercompany",
             money("12000.00")),
        pair(f"{y}-12-30", "Corporate tax provision 2025", "expenses:corporate-tax",
             "liabilities:corporate-tax-payable", money("12450.00"), comment="tax: provision"),
        pair(f"{y}-12-30", "Bank charges for December", "expenses:bank-fees", "assets:cash:bank", money("18.40")),
    ]
    book = Book("ExampleBV", y, "EUR", BV_TITLE, _main_header(BV_TITLE, y, "EUR", 7), BV_ACCOUNTS, files)
    # never the bank account: the corruption probes edit amounts in 2025-bank.journal, and an assertion on that
    # account anywhere would make hledger, rather than the oracle, catch them
    _assert_last(book, "adjustments", "liabilities:corporate-tax-payable")
    _assert_last(book, "custody", "assets:cash:custody")
    return book


def bv_2026(previous: Book) -> Book:
    y = 2026
    opening = opening_from([previous], date=f"{y}-01-01", retained="equity:retained-earnings",
                           description="Opening balances (restating the 2025 closing balance sheet)")
    for p in opening.postings:
        if p.account == "assets:cash:bank":
            p.assertion = p.amount
    files = {name: [] for name in ("opening", "bank", "invoices", "custody", "adjustments", "assertions")}
    files["opening"] = [opening]
    notes = {name: "Nothing booked yet for this year." for name in files if name != "opening"}
    accounts = BV_ACCOUNTS + [("assets:receivables:trade", "new in 2026"), ("income:consulting", "new in 2026")]
    return Book("ExampleBV", y, "EUR", BV_TITLE, _main_header(BV_TITLE, y, "EUR", 7), accounts, files,
                file_notes=notes, commodity_in_main=False, commodity_in_accounts=True)


# Example S.L.
SL_TITLE = "Example S.L."
SL_ACCOUNTS = [
    ("assets:cash:bank", "current account"),
    ("assets:receivables:clients", ""),
    ("assets:receivables:vat", "input VAT"),
    ("assets:fixed:equipment", ""),
    ("assets:fixed:accumulated-depreciation", "contra-asset"),
    ("liabilities:payables:suppliers", ""),
    ("liabilities:vat-payable", "output VAT"),
    ("liabilities:withholding", "income tax withheld from salaries"),
    ("liabilities:social-security", ""),
    ("liabilities:corporate-tax-payable", ""),
    ("equity:share-capital", ""),
    ("equity:retained-earnings", ""),
    ("income:services", ""),
    ("expenses:salaries", ""),
    ("expenses:social-security", ""),
    ("expenses:rent", ""),
    ("expenses:professional-fees", ""),
    ("expenses:bank-fees", ""),
    ("expenses:depreciation", ""),
    ("expenses:corporate-tax", ""),
]
SL_CLIENTS = ["Example Inc.", "Nimbus Travel", "Solana Studio"]


def _sl_year(g: Gen, y: int, months: range, *, opening: Txn | None, constitution: bool) -> Book:
    files: dict[str, list[Txn]] = {}
    if opening is not None:
        files["opening"] = [opening]
    files.update({"bank": [], "invoices": [], "payroll": [], "adjustments": []})
    if constitution:
        files["bank"].append(pair(f"{y}-08-05", "Share capital paid in", "assets:cash:bank", "equity:share-capital",
                                  money("3000.00"), comment="constitution: deed"))
        files["bank"].append(pair(f"{y}-08-19", "Shareholder loan converted to capital", "assets:cash:bank",
                                  "equity:share-capital", money("27000.00")))
    for m in months:
        net = g.amount(6000, 9000)
        vat = money(net * Decimal("0.21"))
        client = g.pick(SL_CLIENTS)
        files["invoices"].append(Txn(g.date(y, m, 2, 20), f"Invoice issued - {client}", [
            Posting("assets:receivables:clients", net + vat),
            Posting("income:services", -net),
            Posting("liabilities:vat-payable", -vat)], comment=f"invoice: F-{y}-{m:02d}"))
        files["bank"].append(pair(g.date(y, m, 21, 27), f"Payment received - {client}", "assets:cash:bank",
                                  "assets:receivables:clients", net + vat))
        rent = money("950.00")
        files["invoices"].append(Txn(g.date(y, m, 2, 6), "Office rent - Calle Mayor Coworking", [
            Posting("expenses:rent", rent), Posting("assets:receivables:vat", money(rent * Decimal("0.21"))),
            Posting("liabilities:payables:suppliers", -(rent + money(rent * Decimal("0.21"))))]))
        files["bank"].append(pair(g.date(y, m, 7, 12), "Rent paid - Calle Mayor Coworking",
                                  "liabilities:payables:suppliers", "assets:cash:bank",
                                  rent + money(rent * Decimal("0.21"))))
        gross = money("3800.00")
        ss_employer, ss_employee, irpf = money("1140.00"), money("241.30"), money("570.00")
        files["payroll"].append(Txn(f"{y}-{m:02d}-27", "Monthly payroll", [
            Posting("expenses:salaries", gross), Posting("expenses:social-security", ss_employer),
            Posting("liabilities:social-security", -(ss_employer + ss_employee)),
            Posting("liabilities:withholding", -irpf),
            Posting("assets:cash:bank", -(gross - ss_employee - irpf))]))
        files["bank"].append(pair(f"{y}-{m:02d}-28", "Social security paid", "liabilities:social-security",
                                  "assets:cash:bank", ss_employer + ss_employee))
        files["bank"].append(pair(g.date(y, m, 2, 27), "Bank maintenance fee", "expenses:bank-fees",
                                  "assets:cash:bank", money("12.10")))
    for q_end in [m for m in months if m % 3 == 0]:
        files["adjustments"].append(pair(f"{y}-{q_end:02d}-28", "Quarterly withholding filed",
                                         "liabilities:withholding", "assets:cash:bank",
                                         irpf * len([m for m in months if (m - 1) // 3 == (q_end - 1) // 3]),
                                         comment="filing: quarterly"))
    files["adjustments"].append(pair(f"{y}-{months[0]:02d}-15", "Laptop purchased", "assets:fixed:equipment",
                                     "assets:cash:bank", money("2400.00")))
    files["adjustments"].append(pair(f"{y}-12-30", "Depreciation for the year", "expenses:depreciation",
                                     "assets:fixed:accumulated-depreciation", money("480.00")))
    files["adjustments"].append(pair(f"{y}-12-30", "Corporate tax provision", "expenses:corporate-tax",
                                     "liabilities:corporate-tax-payable", money("2100.00") if y == 2024 else money("5200.00")))
    if y == 2025:
        files["adjustments"].append(pair(f"{y}-07-20", "Corporate tax 2024 paid", "liabilities:corporate-tax-payable",
                                         "assets:cash:bank", money("2100.00")))
    for name in ("bank", "invoices", "payroll", "adjustments"):
        files[name] = by_date(files[name])
    return Book("ExampleSL", y, "EUR", SL_TITLE, _main_header(SL_TITLE, y, "EUR", 13), SL_ACCOUNTS, files)


def sl_2024(g: Gen) -> Book:
    book = _sl_year(g, 2024, range(8, 13), opening=None, constitution=True)
    held = balances([book])
    # the year end, asserted the way the accountant's export writes it: zero postings carrying the balance
    book.files["assertions"] = [
        Txn("2024-12-31", "Year-end balance check: cash and receivables", [
            Posting(a, Decimal(), assertion=held[a], zero_style=True)
            for a in ("assets:cash:bank", "assets:receivables:clients", "assets:receivables:vat")]),
        Txn("2024-12-31", "Year-end balance check: liabilities", [
            Posting(a, Decimal(), assertion=held[a], zero_style=True)
            for a in ("liabilities:vat-payable", "liabilities:withholding", "liabilities:corporate-tax-payable")]),
    ]
    _assert_last(book, "bank", "liabilities:payables:suppliers")
    return book


def sl_2025(g: Gen, previous: Book) -> Book:
    opening = opening_from([previous], date="2025-01-01", retained="equity:retained-earnings",
                           description="Opening balances (restating the 2024 closing balance sheet)")
    book = _sl_year(g, 2025, range(1, 13), opening=opening, constitution=False)
    book.file_notes["opening"] = "Carried forward from the 2024 annual accounts."
    opening.comment_lines = ["", "Balance sheet as approved with the 2024 annual accounts", ""]
    book.commodity_in_accounts = True
    _assert_last(book, "adjustments", "liabilities:corporate-tax-payable")
    _assert_last(book, "adjustments", "assets:fixed:accumulated-depreciation")
    return book


def sl_2026(previous: list[Book]) -> Book:
    # an annual book restates only its own prior year: the 2025 book alone, whose opening already carries 2024
    opening = opening_from([previous[-1]], date="2026-01-01", retained="equity:retained-earnings",
                           description="Opening balances (restating the 2025 closing balance sheet)")
    for p in opening.postings:
        if p.account == "assets:cash:bank":
            p.assertion = p.amount
    files = {name: [] for name in ("opening", "bank", "invoices", "payroll", "adjustments", "assertions")}
    files["opening"] = [opening]
    notes = {name: "Nothing booked yet for this year." for name in files if name != "opening"}
    return Book("ExampleSL", 2026, "EUR", SL_TITLE, _main_header(SL_TITLE, 2026, "EUR", 13), SL_ACCOUNTS, files,
                file_notes=notes, commodity_in_main=False, commodity_in_accounts=True)


def _main_header(title: str, year: int, currency: str, lines: int) -> list[str]:
    """The comment block at the top of a ``<year>.journal``, padded to ``lines`` lines so the commodity
    directives land on a predictable line (the hledger tests read the directive's line number)."""
    head = [f"; {title} - main journal for fiscal year {year}", ";",
            SAMPLE_NOTE, f"; Every amount in this book is in {currency}."]
    while len(head) < lines - 1:
        head.append(";")
    head.append("")
    return head


def _assert_last(book: Book, group: str, account: str) -> None:
    """Put a balance assertion on the last posting to ``account`` in ``group``, asserting the year-end balance.

    Only valid when no posting to that account is dated later anywhere in the book, which the generator
    guarantees by dating ordinary activity on or before the 28th and year-end entries in this file."""
    total = balances([book])[account]
    last = max((t for t in book.files[group] if any(p.account == account for p in t.postings)), key=lambda t: t.date)
    later = [t for txns in book.files.values() for t in txns if t.date > last.date
             and any(p.account == account for p in t.postings)]
    assert not later, f"{book.entity}/{book.year}: {account} moves after {last.date}"
    next(p for p in last.postings if p.account == account).assertion = total


def build() -> list[Book]:
    g = Gen(20250101)
    inc25 = inc_2025(g)
    bv25 = bv_2025(g)
    sl24 = sl_2024(g)
    sl25 = sl_2025(g, sl24)
    return [inc25, inc_2026(inc25), bv25, bv_2026(bv25), sl24, sl25, sl_2026([sl24, sl25])]


def render(books: list[Book]) -> dict[Path, str]:
    out: dict[Path, str] = {}
    for book in books:
        out.update(write_book(book, ROOT))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="compare instead of writing; exit 1 on a difference")
    args = parser.parse_args(argv)
    files = render(build())
    if args.check:
        on_disk = {p for p in ROOT.rglob("*.journal")}
        stale = sorted(str(p.relative_to(ROOT)) for p in set(files) ^ on_disk)
        stale += sorted(str(p.relative_to(ROOT)) for p in files if p.is_file() and p.read_text(encoding="utf-8") != files[p])
        for name in stale:
            print(f"differs: {name}", file=sys.stderr)
        return 1 if stale else 0
    for path in ROOT.rglob("*.journal"):
        path.unlink()
    for path, text in sorted(files.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} journals under {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
