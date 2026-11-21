"""
Tests for financial money utilities: exact Decimal parsing, integer paise conversion,
precision checks, and non-finite value rejection.
"""

from decimal import Decimal
import pytest
from backend.money import MoneyError, format_paise_to_rupees, parse_rupees_to_paise


def test_rupees_to_paise_exact_conversion_19_99():
    """Verify that ₹19.99 converts exactly to 1999 paise without float rounding errors."""
    assert parse_rupees_to_paise(19.99) == 1999
    assert parse_rupees_to_paise("19.99") == 1999
    assert parse_rupees_to_paise(Decimal("19.99")) == 1999


def test_rupees_to_paise_exact_conversion_430_50():
    """Verify that ₹430.50 converts exactly to 43050 paise."""
    assert parse_rupees_to_paise(430.50) == 43050
    assert parse_rupees_to_paise("430.50") == 43050
    assert parse_rupees_to_paise(Decimal("430.50")) == 43050


def test_rupees_to_paise_integer_values():
    """Verify integer amounts convert cleanly to paise."""
    assert parse_rupees_to_paise(300) == 30000
    assert parse_rupees_to_paise(1) == 100
    assert parse_rupees_to_paise(0) == 0


def test_reject_more_than_two_decimal_places():
    """Verify that fractional paise (> 2 decimal places) are strictly rejected."""
    with pytest.raises(MoneyError, match="cannot have more than two decimal places"):
        parse_rupees_to_paise(10.125)

    with pytest.raises(MoneyError, match="cannot have more than two decimal places"):
        parse_rupees_to_paise("430.555")

    with pytest.raises(MoneyError, match="cannot have more than two decimal places"):
        parse_rupees_to_paise(Decimal("19.999"))


def test_reject_non_finite_values():
    """Verify that NaN, Infinity, and -Infinity are strictly rejected."""
    with pytest.raises(MoneyError, match="must be a finite number"):
        parse_rupees_to_paise(float("nan"))

    with pytest.raises(MoneyError, match="must be a finite number"):
        parse_rupees_to_paise(float("inf"))

    with pytest.raises(MoneyError, match="must be a finite number"):
        parse_rupees_to_paise(float("-inf"))

    with pytest.raises(MoneyError, match="must be a finite number"):
        parse_rupees_to_paise("NaN")

    with pytest.raises(MoneyError, match="must be a finite number"):
        parse_rupees_to_paise("Infinity")


def test_reject_negative_values():
    """Verify that negative amounts are rejected."""
    with pytest.raises(MoneyError, match="cannot be negative"):
        parse_rupees_to_paise(-10)

    with pytest.raises(MoneyError, match="cannot be negative"):
        parse_rupees_to_paise(-19.99)

    with pytest.raises(MoneyError, match="cannot be negative"):
        parse_rupees_to_paise("-430.50")


def test_reject_none_and_empty():
    """Verify None and empty strings are rejected."""
    with pytest.raises(MoneyError, match="cannot be None"):
        parse_rupees_to_paise(None)

    with pytest.raises(MoneyError, match="cannot be empty"):
        parse_rupees_to_paise("   ")


def test_format_paise_to_rupees():
    """Verify integer paise formatting into 2-decimal rupee string."""
    assert format_paise_to_rupees(1999) == "19.99"
    assert format_paise_to_rupees(43050) == "430.50"
    assert format_paise_to_rupees(30000) == "300.00"
    assert format_paise_to_rupees(5) == "0.05"
    assert format_paise_to_rupees(0) == "0.00"
