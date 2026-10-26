"""The books as a document somebody sends somebody else.

``haddon.reports`` answers what the books say.  This package answers what a statement of the books looks like,
and the two are deliberately separate: the figures are computed once, in one place, and nothing here adds to them
or drops any.

* ``period``: what a date range is called, and what came before it.  Both are calendar facts, derived from the
  bounds rather than asked for, so a document cannot be headed one period and hold another.
* ``shape``: sections, groups, subtotals, and the sign a reader expects.  This is the one place the sign
  convention, the grouping and the result-to-date line are decided.
* ``render``: the HTML those become, and the PDF bytes WeasyPrint makes of it.
"""

from haddon.statements.period import (Period, a_year_earlier, classify, comparative, has_begun, preceding,
                                      same_span_last_year, through_today)
from haddon.statements.render import ColumnLabels, DocumentContext, document_html, to_pdf
from haddon.statements.shape import BalanceSheetView, IncomeStatementView, balance_sheet_view, income_statement_view

__all__ = [
    "Period", "classify", "preceding", "comparative", "a_year_earlier", "same_span_last_year",
    "has_begun", "through_today",
    "IncomeStatementView", "BalanceSheetView", "income_statement_view", "balance_sheet_view",
    "ColumnLabels", "DocumentContext", "document_html", "to_pdf",
]
