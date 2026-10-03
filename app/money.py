"""All money in this app is US dollars. Demo numbers, not real prices."""

CURRENCY_CODE = "USD"
CURRENCY_SYMBOL = "$"


def money(amount: float | int | None) -> str:
    """12.5 -> '$12.50', -29.4 -> '-$29.40', None -> 'n/a'."""
    if amount is None:
        return "n/a"
    sign = "-" if amount < 0 else ""
    return f"{sign}{CURRENCY_SYMBOL}{abs(amount):,.2f}"
