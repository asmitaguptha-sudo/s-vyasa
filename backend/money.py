"""
Financial money utilities for Vaada Khata.
Guarantees exact calculations using integer paise and Decimal,
rejecting floating-point imprecision, non-finite values, and invalid precision.
"""

from decimal import Decimal, InvalidOperation
import math
from typing import Any, Union


class MoneyError(ValueError):
    """Raised when an invalid monetary amount is encountered."""
    pass


def parse_rupees_to_paise(amount: Union[Decimal, int, float, str, Any]) -> int:
    """
    Safely convert rupee amounts to integer paise (1 Rupee = 100 Paise).

    Guarantees:
    - Rejects None and empty values.
    - Rejects non-finite values (NaN, Infinity, -Infinity).
    - Rejects negative values (amount must be >= 0).
    - Rejects precision greater than 2 decimal places (no fractional paise allowed).
    - Performs exact conversion via Decimal without binary float rounding errors.
    """
    if amount is None:
        raise MoneyError("Amount cannot be None.")

    if isinstance(amount, float):
        if math.isnan(amount) or math.isinf(amount):
            raise MoneyError(f"Amount must be a finite number, got {amount}.")
        str_val = str(amount)
    elif isinstance(amount, (int, Decimal)):
        str_val = str(amount)
    elif isinstance(amount, str):
        str_val = amount.strip()
        if not str_val:
            raise MoneyError("Amount string cannot be empty.")
    else:
        str_val = str(amount).strip()

    try:
        dec = Decimal(str_val)
    except InvalidOperation as exc:
        raise MoneyError(f"Invalid monetary format: {amount!r}.") from exc

    if not dec.is_finite():
        raise MoneyError(f"Amount must be a finite number, got {amount}.")

    if dec < 0:
        raise MoneyError(f"Amount cannot be negative: {amount}.")

    # Verify decimal places: exponent should not be smaller than -2
    exp = dec.as_tuple().exponent
    if isinstance(exp, int) and exp < -2:
        raise MoneyError(f"Amount cannot have more than two decimal places: {amount}.")

    return int(dec * 100)


def format_paise_to_rupees(paise: int) -> str:
    """
    Format integer paise into a standard two-decimal rupee string (e.g. 1999 -> '19.99').
    """
    if not isinstance(paise, int) or isinstance(paise, bool):
        raise MoneyError("Paise value must be an integer.")

    sign = "-" if paise < 0 else ""
    abs_paise = abs(paise)
    rupees, rem = divmod(abs_paise, 100)
    return f"{sign}{rupees}.{rem:02d}"
