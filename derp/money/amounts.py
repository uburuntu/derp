"""Derpcoins in whole dc units, precise to a millionth. Telegram Stars stay integral."""

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Numeric

DC = Numeric(25, 6)  # retains the entire historical BIGINT range
QUANTUM = Decimal("0.000001")
Amount = Decimal | int


def exact_dc(value: Amount) -> Decimal:
    """Reject sub-microcoin movements before different ledger legs can round independently."""
    amount = Decimal(value)
    if not amount.is_finite() or amount != amount.quantize(QUANTUM):
        raise ValueError("Derpcoin amounts must have at most six decimal places")
    return amount


def priced(usd: Decimal | float, markup: Decimal, unit_usd: Decimal) -> Decimal:
    """Round once at settlement. Rounding a provider call to a full coin would inflate tiny charges."""
    return ((Decimal(str(usd)) * markup) / unit_usd).quantize(QUANTUM, rounding=ROUND_HALF_UP)


def format_dc(value: Amount) -> str:
    """Grouped whole coins and only significant fractional places, never scientific notation."""
    text = format(Decimal(value), ",f")
    return text.rstrip("0").rstrip(".") if "." in text else text
