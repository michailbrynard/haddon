"""Result shapes for the reports.

Every amount is a ``Micros``; nothing here is ever a float or a merged cross-book total.  A journal is continuous,
so a result names the date range it was computed over.
"""

from __future__ import annotations

from dataclasses import dataclass

from haddon.domain.micros import ZERO, Micros


@dataclass(frozen=True)
class Line:
    """One (account, commodity) amount: a balance or a period movement, depending on the report."""

    account_id: str
    path: str
    type: str
    commodity: str
    amount: Micros


def _index(lines: tuple[Line, ...]) -> dict[tuple[str, str], Micros]:
    return {(ln.account_id, ln.commodity): ln.amount for ln in lines}


@dataclass(frozen=True)
class TrialBalance:
    through: str | None
    lines: tuple[Line, ...]

    def get(self, account_id: str, commodity: str) -> Micros:
        """Balance of one account/commodity through this report's date (``ZERO`` when the pair has no posted amount)."""
        return _index(self.lines).get((account_id, commodity), ZERO)

    def of_type(self, *types: str) -> tuple[Line, ...]:
        return tuple(ln for ln in self.lines if ln.type in types)


@dataclass(frozen=True)
class Movements:
    """Period movements over a date range, opening entries excluded."""

    start: str | None
    end: str | None
    lines: tuple[Line, ...]

    def get(self, account_id: str, commodity: str) -> Micros:
        return _index(self.lines).get((account_id, commodity), ZERO)


@dataclass(frozen=True)
class IncomeStatement:
    start: str | None
    end: str | None
    lines: tuple[Line, ...]
    net: dict[str, Micros]  # per commodity: sum of income and expense movements (negative = profit, debit-positive signs)


@dataclass(frozen=True)
class RegisterLine:
    entry_id: str
    dt: str
    seq: int
    narration: str
    origin: str
    account_id: str
    path: str
    commodity: str
    amount: Micros
    memo: str | None
    tax_code: str | None
    running: Micros | None  # running balance, only when the register is for one account


@dataclass(frozen=True)
class Register:
    start: str | None
    end: str | None
    account_id: str | None
    lines: tuple[RegisterLine, ...]
