from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta

TWOPLACES = Decimal("0.01")
FOURPLACES = Decimal("0.0001")


def D(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value) -> Decimal:
    return D(value).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def units_qty(value) -> Decimal:
    return D(value).quantize(FOURPLACES, rounding=ROUND_HALF_UP)


def format_inr(num) -> str:
    amount = money(num)
    negative = amount < 0
    amount = abs(amount)
    whole, frac = f"{amount:.2f}".split(".")
    if len(whole) <= 3:
        grouped = whole
    else:
        last3 = whole[-3:]
        rest = whole[:-3]
        parts = []
        while rest:
            parts.append(rest[-2:])
            rest = rest[:-2]
        grouped = ",".join(reversed(parts)) + "," + last3
    text = f"₹{grouped}.{frac}"
    return f"-{text}" if negative else text


def format_pct(num) -> str:
    value = D(num).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    sign = "+" if value > 0 else ""
    return f"{sign}{value}%"


def calc_units(amount, nav) -> Decimal:
    amount = D(amount)
    nav = D(nav)
    if nav <= 0:
        raise ValueError("NAV must be greater than 0.")
    return units_qty(amount / nav)


def monthly_equivalent(amount, frequency: str) -> Decimal:
    amount = D(amount)
    if frequency == "Monthly":
        return money(amount)
    if frequency == "Quarterly":
        return money(amount / Decimal("3"))
    if frequency == "Weekly":
        return money(amount * Decimal("52") / Decimal("12"))
    return money(amount)


def next_due_after(due: date, frequency: str) -> date:
    if frequency == "Weekly":
        return due + timedelta(days=7)
    if frequency == "Quarterly":
        return due + relativedelta(months=3)
    return due + relativedelta(months=1)


def apply_holding(holding, txn_type: str, amount, units):
    amount = D(amount)
    units = D(units)
    current_units = D(holding["units"] if holding else 0)
    invested = D(holding["invested_amount"] if holding else 0)

    if txn_type in ("SIP", "Purchase"):
        return {
            "units": units_qty(current_units + units),
            "invested_amount": money(invested + amount),
        }

    if units > current_units + Decimal("0.00005"):
        raise ValueError(
            f"Insufficient units for redemption. Available: {current_units:.2f} Requested: {units:.2f}"
        )
    if current_units <= 0:
        raise ValueError("No units available to redeem.")

    invested_out = money(invested * units / current_units)
    remaining_units = units_qty(current_units - units)
    remaining_invested = money(invested - invested_out)
    if remaining_units <= 0:
        remaining_units = units_qty(0)
        remaining_invested = money(0)
    return {"units": remaining_units, "invested_amount": remaining_invested}


def progress_pct(current, target) -> float:
    target = D(target)
    if target <= 0:
        return 0.0
    pct = (D(current) / target) * Decimal("100")
    if pct > 100:
        pct = Decimal("100")
    if pct < 0:
        pct = Decimal("0")
    return float(pct.quantize(TWOPLACES, rounding=ROUND_HALF_UP))
