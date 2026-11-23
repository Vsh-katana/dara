"""Exact monetary parsing and serialization helpers."""
from decimal import Decimal, InvalidOperation


class MoneyError(ValueError):
    pass


def decimal_value(value, *, allow_negative: bool = True) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise MoneyError("A numeric amount is required.")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        raise MoneyError("Amount must be a valid decimal number.") from None
    if not number.is_finite():
        raise MoneyError("Amount must be finite.")
    if not allow_negative and number < 0:
        raise MoneyError("Amount cannot be negative.")
    if number.as_tuple().exponent < -8:
        raise MoneyError("Amount supports up to 8 decimal places.")
    return number


def money_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def add(values) -> Decimal:
    total = Decimal("0")
    for value in values:
        total += decimal_value(value)
    return total
