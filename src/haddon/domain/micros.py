"""Exact amounts (spec "Accounting and transaction contract").

``Micros`` is a signed integer count of millionths: ``1 USD = 1000000``.  Amounts cross the wire as base-10
integer strings (``Micros.parse`` / ``Micros.render``), never as floats or JSON numbers.  Decimal readers
convert with ``Micros.from_decimal`` which is exact or refuses: fractional micros, unsupported reader precision,
non-finite values, signed-64-bit overflow (``-2**63`` included, decisions.md U6) and unknown commodities are all
422 errors; nothing is ever rounded.

Both string forms are canonical, and that is published.  Each accepts exactly one spelling of each amount: ASCII
digits, no surrounding whitespace of any kind, no leading zeros, no ``+``, and one spelling of zero.  The
published ``amount_contract`` in ``/capabilities`` is this paragraph, and the 0006 review found the inbound half
advertising a strictness it did not have.  Report accumulation uses Python ints via ``sum_micros`` and re-checks the
range, so an aggregate that would overflow SQLite is an error here too.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from haddon.domain.errors import OVERFLOW, PRECISION, VALIDATION, HaddonError

INT64_MAX = 2**63 - 1
MIN_MICROS = -INT64_MAX  # -2**63 is excluded on every amount column (U6); abs() of it would overflow
MAX_MICROS = INT64_MAX
MAX_PRECISION = 6
UNIT = 10**MAX_PRECISION

_INT_STRING = re.compile(r"\A-?(0|[1-9][0-9]*)\Z")

#: The inbound wire form, and the same canonical shape ``_INT_STRING`` demands with a fraction allowed after it.
#: Three details are deliberate, and each was a hole ``/capabilities`` already said was closed (the 0006 review):
#: ``\A``/``\Z`` rather than ``^``/``$``, because ``$`` also matches before a trailing newline and ``"12.34\n"``
#: was accepted while ``"12.34 "`` was refused; ``[0-9]`` rather than ``\d``, because ``\d`` is every Unicode
#: decimal digit and ``"١٢.٣٤"`` silently became 12.34; and ``0|[1-9][0-9]*`` rather than ``\d+``, because
#: ``"0012.00"`` is not one amount written one way.
_DECIMAL_STRING = re.compile(r"\A-?(0|[1-9][0-9]*)(\.[0-9]+)?\Z")


def quantum_for(precision: int) -> int:
    """Smallest representable micro step for a commodity with ``precision`` decimals (the schema's CHECK table)."""
    if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= MAX_PRECISION:
        raise HaddonError(PRECISION, f"unsupported precision {precision!r}; 0..{MAX_PRECISION} decimals are supported",
                          details=[{"path": "precision", "reason": "unsupported_precision", "value": precision}])
    return 10 ** (MAX_PRECISION - precision)


@dataclass(frozen=True)
class Commodity:
    code: str
    precision: int

    @property
    def quantum(self) -> int:
        return quantum_for(self.precision)


def _overflow(value: int, path: str) -> HaddonError:
    return HaddonError(OVERFLOW, f"{path}: {value} is outside the signed 64-bit range",
                       details=[{"path": path, "reason": "int64_overflow", "value": str(value),
                                 "min": str(MIN_MICROS), "max": str(MAX_MICROS)}])


@dataclass(frozen=True, order=True)
class Micros:
    """An exact signed amount in millionths.  Construction validates type and range; never built from a float."""

    value: int

    def __post_init__(self) -> None:
        v = self.value
        if isinstance(v, bool) or not isinstance(v, int):
            raise HaddonError(VALIDATION, f"micros must be an int, got {type(v).__name__}",
                              details=[{"path": "amount_micros", "reason": "int_required", "type": type(v).__name__}])
        if not MIN_MICROS <= v <= MAX_MICROS:
            raise _overflow(v, "amount_micros")

    # Wire format: base-10 integer strings.
    @classmethod
    def parse(cls, text: object, *, path: str = "amount_micros") -> "Micros":
        """Parse a canonical base-10 integer string.

        Anything that is not a ``str`` (a JSON number, a float, an int) is refused, as are signs other than a single
        leading ``-``, whitespace, leading zeros and ``-0``.
        """
        if not isinstance(text, str):
            raise HaddonError(VALIDATION, f"{path}: amounts are base-10 integer strings, got {type(text).__name__}",
                              details=[{"path": path, "reason": "string_required", "type": type(text).__name__}])
        if not _INT_STRING.match(text) or text == "-0":
            raise HaddonError(VALIDATION, f"{path}: {text!r} is not a canonical base-10 integer string",
                              details=[{"path": path, "reason": "not_integer_string", "value": text}])
        v = int(text)
        if not MIN_MICROS <= v <= MAX_MICROS:
            raise _overflow(v, path)
        return cls(v)

    def render(self) -> str:
        return str(self.value)

    def __str__(self) -> str:
        return self.render()

    @classmethod
    def from_decimal(cls, amount: object, *, commodity: Commodity | None = None, path: str = "amount") -> "Micros":
        """Convert a decimal amount in whole units (``"12.50"``) to micros, exactly or not at all.

        Refuses floats (type), non-finite values, more than 6 decimal places (fractional micros), more decimal
        places than the commodity supports (unsupported reader precision), amounts that are not a multiple of the
        commodity quantum, and int64 overflow.  Trailing zeros count as declared places: a reader that emits
        ``"1.500"`` for a 2-decimal commodity is misconfigured, and that is reported rather than normalised.
        """
        if isinstance(amount, float):
            raise HaddonError(VALIDATION, f"{path}: float amounts are refused; pass a decimal string",
                              details=[{"path": path, "reason": "float_rejected"}])
        if isinstance(amount, Decimal):
            d = amount
        elif isinstance(amount, str):
            if not _DECIMAL_STRING.match(amount):
                raise HaddonError(VALIDATION, f"{path}: {amount!r} is not a canonical decimal string",
                                  details=[{"path": path, "reason": "decimal_shape", "value": amount}])
            if amount.startswith("-") and not any(c in "123456789" for c in amount):
                raise HaddonError(VALIDATION, f"{path}: {amount!r} is negative zero; zero has one spelling",
                                  details=[{"path": path, "reason": "negative_zero", "value": amount}])
            try:
                d = Decimal(amount)
            except InvalidOperation:  # pragma: no cover - the regex already excludes this
                raise HaddonError(VALIDATION, f"{path}: {amount!r} is not a decimal", details=[{"path": path}]) from None
        else:
            raise HaddonError(VALIDATION, f"{path}: decimal string or Decimal required, got {type(amount).__name__}",
                              details=[{"path": path, "reason": "decimal_required", "type": type(amount).__name__}])
        if not d.is_finite():
            raise HaddonError(VALIDATION, f"{path}: non-finite amount {str(d)!r}",
                              details=[{"path": path, "reason": "non_finite", "value": str(d)}])
        exponent = d.as_tuple().exponent
        places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
        if places > MAX_PRECISION:
            raise HaddonError(PRECISION, f"{path}: {str(d)!r} has {places} decimal places; micros carry at most {MAX_PRECISION}",
                              details=[{"path": path, "reason": "fractional_micros", "places": places, "value": str(d)}])
        if commodity is not None and places > commodity.precision:
            raise HaddonError(PRECISION, f"{path}: {str(d)!r} has {places} decimal places; {commodity.code} supports {commodity.precision}",
                              details=[{"path": path, "reason": "unsupported_precision", "places": places,
                                        "commodity": commodity.code, "precision": commodity.precision, "value": str(d)}])
        scaled = d.scaleb(MAX_PRECISION)
        if scaled != scaled.to_integral_value():  # pragma: no cover - excluded by the places check
            raise HaddonError(PRECISION, f"{path}: fractional micros", details=[{"path": path, "reason": "fractional_micros"}])
        v = int(scaled)
        if not MIN_MICROS <= v <= MAX_MICROS:
            raise _overflow(v, path)
        m = cls(v)
        if commodity is not None:
            m.check_quantum(commodity, path=path)
        return m

    def check_quantum(self, commodity: Commodity, *, path: str = "amount_micros") -> "Micros":
        q = commodity.quantum
        if self.value % q != 0:
            raise HaddonError(PRECISION, f"{path}: {self.value} is not a multiple of the {commodity.code} quantum {q}",
                              details=[{"path": path, "reason": "quantum", "commodity": commodity.code, "quantum": q,
                                        "value": self.render()}])
        return self

    def to_decimal_string(self, commodity: Commodity) -> str:
        """Render in whole units with exactly the commodity's decimal places (exact, or a ``precision`` error)."""
        self.check_quantum(commodity)
        sign = "-" if self.value < 0 else ""
        whole, frac = divmod(abs(self.value), UNIT)
        if commodity.precision == 0:
            return f"{sign}{whole}"
        digits = f"{frac:06d}"[: commodity.precision]
        return f"{sign}{whole}.{digits}"

    # Arithmetic is exact and range-checked by the constructor.
    def __neg__(self) -> "Micros":
        return Micros(-self.value)

    def __add__(self, other: "Micros") -> "Micros":
        return Micros(self.value + _val(other))

    def __sub__(self, other: "Micros") -> "Micros":
        return Micros(self.value - _val(other))

    def __int__(self) -> int:
        return self.value

    def __bool__(self) -> bool:
        return self.value != 0

    @property
    def is_zero(self) -> bool:
        return self.value == 0


def _val(other: object) -> int:
    if isinstance(other, Micros):
        return other.value
    raise TypeError(f"Micros arithmetic needs Micros, got {type(other).__name__}")


def sum_micros(values: Iterable[int | Micros], *, path: str = "total") -> Micros:
    """Sum amounts in Python ints for a report.  The total must itself fit the amount range."""
    total = 0
    for v in values:
        total += v.value if isinstance(v, Micros) else int(v)
    if not MIN_MICROS <= total <= MAX_MICROS:
        raise _overflow(total, path)
    return Micros(total)


ZERO = Micros(0)
