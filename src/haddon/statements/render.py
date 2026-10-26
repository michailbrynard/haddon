"""The statements as a document: HTML for WeasyPrint, and the PDF bytes that come out of it.

A PDF is not a screenshot of a page.  It is emailed, filed and forwarded, and it outlives the context it was
made in.  Three things follow, and they are why this module exists rather than a print stylesheet:

* It is produced by the service, not by a browser.  An agent asks for a statement the same way a person does,
  and neither needs a rendering engine of their own.
* It says what it is.  An open book with no close behind it is management information, not a filed account,
  and the document says so on its face along with the date it was drawn.  A handsome document that quietly
  reads as final is the failure mode worth engineering against.
* It carries its own checks.  Assets against liabilities and equity, at every date shown, and the composition
  of the period, are stated on the page rather than computed and discarded.  A statement that shows it balances
  is worth more than one that asserts it.

Typography is the rest of it: one type family with tabular figures, figures right aligned on a shared decimal
position, subtotals ruled and totals double ruled, section headings that cannot be separated from their first
line, and a running header and ``Page N of M`` on every page so a printout that gets shuffled can be put back
together.  The fonts are named from what this box actually has (``fc-list``); a missing family substitutes
silently and takes the figure alignment with it, so the stack ends at a generic and starts at two that exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape

from haddon.domain.micros import Commodity, Micros
from haddon.statements.shape import (BalanceSheetView, IncomeStatementView, StatementSection,
                                     income_result_label, omitted_commodities_note)

MONTHS = ("January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December")
SHORT_MONTHS = tuple(m[:3] for m in MONTHS)


def date_words(date: str) -> str:
    y, m, d = date.split("-")
    return f"{int(d)} {MONTHS[int(m) - 1]} {y}"


def short_date_words(date: str) -> str:
    y, m, d = date.split("-")
    return f"{int(d)} {SHORT_MONTHS[int(m) - 1]} {y}"


def range_words(start: str, end: str) -> str:  # noqa: D401 - see haddon.statements.period._range_words
    """A range with the year said once when both ends share it: ``1 April – 30 June 2026``."""
    ys, ms, ds = start.split("-")
    ye, me, de = end.split("-")
    left = f"{int(ds)} {MONTHS[int(ms) - 1]}" if ys == ye else f"{int(ds)} {MONTHS[int(ms) - 1]} {ys}"
    return f"{left} – {int(de)} {MONTHS[int(me) - 1]} {ye}"


def money(amount: Micros | None, commodity: Commodity) -> str:
    """Format whole units at the commodity's precision, thousands separated.

    The conversion is ``Micros.to_decimal_string``, exact or an error; the only thing added here is the
    separators.
    """
    if amount is None:
        return ""
    text = amount.to_decimal_string(commodity)
    negative = text.startswith("-")
    body = text[1:] if negative else text
    whole, _, fraction = body.partition(".")
    grouped = f"{int(whole):,}"
    return f"{'-' if negative else ''}{grouped}{'.' + fraction if fraction else ''}"


@dataclass(frozen=True)
class ColumnLabels:
    """What the two figure columns are called.

    Short enough for a 30mm column, and named by whoever chose the period: a quarter is ``Q2 2026`` and an
    arbitrary range is its own dates, and the view cannot tell which it has.
    """

    current: str
    comparative: str | None
    #: the long form under the income statement's title.  It comes from the caller because only the caller
    #: knows whether the period had finished when it was asked for, and a heading that claimed a whole quarter
    #: of a quarter still running is the one thing a document that outlives its request must not do.
    heading: str


@dataclass(frozen=True)
class DocumentContext:
    """Everything on the document that is not a figure, all of it read from the record."""

    entity_id: str
    legal_name: str
    legal_form: str
    country: str
    commodity: Commodity
    period_start: str          # the period's own first day; the document names its range, not a book year
    lock_through: str | None  # the latest closed date, or None when nothing is closed
    generated_at: str         # in words, UTC
    #: commodities these books hold in this period that this document does not show.  A statement is of one
    #: commodity, since amounts in two of them cannot be added and a total that tried would be a lie.  So the
    #: honest thing is to name what has been left out rather than to let the document read as the whole of it.
    other_commodities: tuple[str, ...] = ()


# Two families this box actually has (`fc-list`), then a generic.  Liberation Sans is metric-compatible with
# Arial and its digits are tabular, which is what keeps a column of figures on one decimal position; naming a
# family that is not installed substitutes silently and takes the alignment with it.
STYLE = """
@page {
  size: A4;
  margin: 24mm 18mm 20mm;
  @top-left { content: string(entity); font-size: 7.5pt; color: #555; letter-spacing: .02em; }
  @top-right { content: string(period); font-size: 7.5pt; color: #555; letter-spacing: .02em; }
  @bottom-left { content: string(drawn); font-size: 7.5pt; color: #555; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font-size: 7.5pt; color: #555; }
}
@page :first { @top-left { content: ""; } @top-right { content: ""; } }

html { font-family: "Liberation Sans", "DejaVu Sans", sans-serif; font-size: 9.5pt; color: #14110f; }
body { margin: 0; }

.head-strings { string-set: entity content(); position: absolute; visibility: hidden; height: 0; }

h1 { font-size: 16pt; margin: 0 0 1mm; letter-spacing: -.01em; }
.subtitle { font-size: 11pt; margin: 0 0 1mm; color: #14110f; }
.meta { font-size: 8.5pt; color: #555; margin: 0; }

.rule { border-bottom: .6pt solid #14110f; margin: 4mm 0 5mm; }

/* A statement flows: rows and headings hold together (`tr`, `break-after: avoid`) and the column
   heads repeat on the next page, which is how a long balance sheet is meant to carry over.  Keeping
   a whole statement together instead would push a short one onto its own page and leave the one
   before it half empty. */
section.statement { margin-bottom: 8mm; }
h2 { font-size: 11.5pt; letter-spacing: .06em; text-transform: uppercase; margin: 0 0 .5mm;
     break-after: avoid; }
h2 + .period { font-size: 9.5pt; color: #444; margin: 0 0 2mm; break-after: avoid; }

table { width: 100%; border-collapse: collapse; font-size: 9.5pt; }
thead { display: table-header-group; }
thead th { font-size: 7.5pt; letter-spacing: .06em; text-transform: uppercase; color: #555;
           border-bottom: .6pt solid #14110f; padding: 0 0 1.2mm; font-weight: normal; }
thead th.figure { text-align: right; }
tr { break-inside: avoid; }
tbody.section { break-inside: avoid; }
tbody.close { break-before: avoid; }

th.section { text-align: left; font-size: 8pt; letter-spacing: .08em; text-transform: uppercase;
             padding: 3.5mm 0 1mm; font-weight: bold; break-after: avoid; }
th.group { text-align: left; font-weight: bold; padding: 1.6mm 0 .4mm; font-weight: normal;
           font-style: italic; break-after: avoid; }
th.item { text-align: left; font-weight: normal; padding: .9mm 0; }
th.item.indent { padding-left: 5mm; }

td.figure { text-align: right; padding: .9mm 0 .9mm 8mm; width: 30mm;
            font-variant-numeric: tabular-nums lining-nums; font-feature-settings: "tnum" 1, "lnum" 1; }
td.figure.prior { color: #444; }
tr.subtotal td.figure { border-top: .4pt solid #999; }
tr.subtotal th.item { padding-left: 5mm; color: #555; }
tr.total { break-before: avoid; }
tr.total th.item { font-weight: bold; padding-top: 1.4mm; }
tr.total td.figure { border-top: .6pt solid #14110f; font-weight: bold; padding-top: 1.4mm; }
tr.result { break-before: avoid; }
tr.result th.item { font-weight: bold; padding-top: 2mm; }
tr.result td.figure { border-top: .6pt solid #14110f; border-bottom: 2.2pt double #14110f;
                      font-weight: bold; padding-top: 2mm; padding-bottom: 1.4mm; }

.check { font-size: 8.5pt; margin-top: 3mm; padding: 2mm 3mm; break-inside: avoid;
         border-left: 2pt solid #2f6b4f; background: #f4f8f5; }
.check.off { border-left-color: #9b2c2c; background: #fdf4f4; }

.subtitle .draft { font-size: 8pt; letter-spacing: .1em; text-transform: uppercase;
                     border: .6pt solid #b8860b; color: #8a6512; padding: .4mm 1.6mm;
                     border-radius: 1mm; margin-left: 2.5mm; vertical-align: .8mm; }
.footnote { font-size: 8pt; line-height: 1.5; color: #555; margin: 6mm 0 0; break-inside: avoid; }
"""


def _figure(amount: Micros | None, commodity: Commodity, *, prior: bool = False) -> str:
    classes = "figure prior" if prior else "figure"
    return f'<td class="{classes}">{escape(money(amount, commodity))}</td>'


def _section_rows(section: StatementSection, commodity: Commodity, *, show_comparative: bool) -> str:
    span = 3 if show_comparative else 2
    out = [f'<tbody class="section"><tr><th class="section" colspan="{span}">{escape(section.title)}</th></tr>']
    for group in section.groups:
        indent = ""
        if group.heading:
            indent = " indent"
            out.append(f'<tr><th class="group" colspan="{span}">{escape(group.heading)}</th></tr>')
        for line in group.lines:
            out.append(f'<tr><th class="item{indent}">{escape(line.label)}</th>'
                       f"{_figure(line.amount, commodity)}"
                       f"{_figure(line.comparative, commodity, prior=True) if show_comparative else ''}</tr>")
        if group.subtotal is not None:
            out.append(f'<tr class="subtotal"><th class="item">Total {escape(group.heading or "")}</th>'
                       f"{_figure(group.subtotal, commodity)}"
                       f"{_figure(group.comparative_subtotal, commodity, prior=True) if show_comparative else ''}</tr>")
    out.append(f'<tr class="total"><th class="item">{escape(section.total_label)}</th>'
               f"{_figure(section.total, commodity)}"
               f"{_figure(section.comparative_total, commodity, prior=True) if show_comparative else ''}</tr>"
               "</tbody>")
    return "".join(out)


def _head(current: str, comparative: str | None) -> str:
    prior = f'<th class="figure">{escape(comparative)}</th>' if comparative else ""
    return (f"<thead><tr><th>Account</th><th class=\"figure\">{escape(current)}</th>{prior}</tr></thead>")


def income_statement_html(view: IncomeStatementView, commodity: Commodity, labels: ColumnLabels) -> str:
    show = view.show_comparative
    body = "".join(_section_rows(s, commodity, show_comparative=show) for s in view.sections)
    result_label = income_result_label(view)
    result = (f'<tr class="result"><th class="item">{escape(result_label)}</th>'
              f"{_figure(view.result, commodity)}"
              f"{_figure(view.comparative_result, commodity, prior=True) if show else ''}</tr>")
    return (
        '<section class="statement">'
        "<h2>Income statement</h2>"
        f'<p class="period">For the period {escape(labels.heading)}</p>'
        "<table>"
        f"{_head(labels.current, labels.comparative if show else None)}"
        f"{body}<tbody class=\"close\">{result}</tbody>"
        "</table></section>"
    )


def balance_sheet_html(view: BalanceSheetView, commodity: Commodity) -> str:
    show = view.show_comparative
    body = "".join(_section_rows(s, commodity, show_comparative=show) for s in view.sections)
    funding = (f'<tr class="result"><th class="item">Total liabilities and equity</th>'
               f"{_figure(view.total_funding, commodity)}"
               f"{_figure(view.comparative_total_funding, commodity, prior=True) if show else ''}</tr>")
    off = [(when, d) for when, d in view.checks if not d.is_zero]
    if off:
        check = "".join(f"<div>At {escape(date_words(w))}, assets and the two sides funding them differ by "
                        f"{escape(money(d, commodity))} {escape(commodity.code)}.</div>" for w, d in off)
        check = f'<div class="check off">{check}</div>'
    else:
        where = " at both dates" if len(view.checks) > 1 else ""
        check = (f'<div class="check">Assets equal liabilities plus equity{where}, '
                 f"including the result to date carried in equity.</div>")
    return (
        '<section class="statement">'
        "<h2>Balance sheet</h2>"
        f'<p class="period">As at {escape(date_words(view.as_of))}</p>'
        "<table>"
        f"{_head(short_date_words(view.as_of), short_date_words(view.comparative_as_of) if show else None)}"
        f"{body}<tbody class=\"close\">{funding}</tbody>"
        f"</table>{check}</section>"
    )


def _footnote(ctx: DocumentContext) -> str:
    """Render the omitted-commodity line, or nothing at all.

    This is the one thing a reader of this document cannot get from the figures on it: the page shows one
    commodity, and amounts in another cannot be added to these.  The sentence lives in
    ``shape.omitted_commodities_note`` so the PDF and ``/reports/statements`` cannot name different omissions.
    The sign convention is on the JSON face only, because a statement is a form its reader already knows how to
    read.  A document with nothing left out gets no footnote rather than an empty rule of grey text.
    """
    note = omitted_commodities_note(ctx.other_commodities)
    return f"<p class=\"footnote\">{escape(note)}</p>" if note else ""


def document_html(ctx: DocumentContext, income: IncomeStatementView, sheet: BalanceSheetView,
                  labels: ColumnLabels) -> str:
    """Render the whole document, ready for WeasyPrint."""
    c = ctx.commodity
    period = labels.heading
    running = f"Income statement and balance sheet · {period}"
    # Draft while the period is not closed: a statement of a book still open to posting is a draft as a matter
    # of record, not of opinion, and the marking follows `lock_through` rather than being chosen.
    mark = "" if ctx.lock_through is not None and ctx.lock_through >= sheet.as_of else " — draft"
    mark_html = '<span class="draft">draft</span>' if mark else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(ctx.legal_name)} — statements</title>
<style>{STYLE}
.strings {{ height: 0; overflow: hidden; }}
#s-entity {{ string-set: entity content(); }}
#s-period {{ string-set: period content(); }}
#s-drawn {{ string-set: drawn content(); }}
</style></head><body>
<div class="strings" aria-hidden="true">
  <div id="s-entity">{escape(ctx.legal_name)}{escape(mark)}</div>
  <div id="s-period">{escape(running)}</div>
  <div id="s-drawn">Drawn {escape(ctx.generated_at)}</div>
</div>

<h1>{escape(ctx.legal_name)}</h1>
<p class="subtitle">Income statement and balance sheet{mark_html}</p>
<p class="meta">For the period {escape(period)} · balance sheet as at {escape(date_words(sheet.as_of))}<br>
All amounts in {escape(c.code)} · {escape(ctx.legal_form)} · {escape(ctx.country)}</p>
<div class="rule"></div>

{income_statement_html(income, c, labels)}
{balance_sheet_html(sheet, c)}
{_footnote(ctx)}
</body></html>"""


def to_pdf(html: str) -> bytes:
    """Render HTML to PDF bytes.

    The import is local: WeasyPrint pulls in Pango and friends, and a service that only ever serves JSON should
    not pay for that at start-up.
    """
    from weasyprint import HTML  # noqa: PLC0415 - deliberate, see above

    return HTML(string=html).write_pdf()


def drawn_words(now: datetime | None = None) -> str:
    """Say when a document was drawn, in the words its footer uses: ``12 September 2026, 13:05 UTC``.

    A statement of an open book is a snapshot, and two copies drawn an hour apart are two different documents.
    This is what tells them apart on the face of the document; the event sequence the figures were read at is
    on the JSON faces (``snapshot_seq``) and deliberately not on the page, which is a document a reader reads
    rather than a record of this service's internals.
    """
    at = now or datetime.now(tz=timezone.utc)
    return f"{at.day} {MONTHS[at.month - 1]} {at.year}, {at:%H:%M} UTC"
