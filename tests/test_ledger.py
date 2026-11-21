"""
Unit tests for Core Ledger Engine (Milestone 2).
Covers:
1. Adding credit with validation.
2. Recording payments with payment method validation.
3. Authoritative outstanding balance calculation.
4. Transaction history preservation.
5. Atomic writes and payload-bound idempotency.
6. Safe additive compensating reversals.
7. Persistence across database reconnection.
"""

import os
import tempfile
import sqlite3
import pytest

from backend.database import SCHEMA_SQL, get_db_connection, run_migrations
import backend.ledger as ledger
import backend.customer_service as customer_service
from backend.schemas import CustomerCreate


@pytest.fixture
def db():
    """Provides an isolated in-memory database with schema and migrations."""
    conn = get_db_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def sample_customer(db):
    """Creates a sample customer record."""
    return customer_service.create_customer(db, CustomerCreate(name="Ramesh", alias="Auto wale"))


def test_add_credit_success(db, sample_customer):
    """Test recording credit persistently for a valid customer."""
    tx = ledger.record_credit(
        db,
        customer_id=sample_customer.customer_id,
        amount_paise=30000,
        item="Wheat Flour 10kg",
    )
    assert tx.transaction_id is not None
    assert tx.customer_id == sample_customer.customer_id
    assert tx.type == "CREDIT"
    assert tx.amount_paise == 30000
    assert tx.amount_rupees == 300.0
    assert tx.item == "Wheat Flour 10kg"
    assert tx.status == "COMMITTED"

    # Verify balance directly
    bal = ledger.get_customer_balance(db, sample_customer.customer_id)
    assert bal["balance_paise"] == 30000
    assert bal["balance_rupees"] == 300.0


def test_add_credit_nonexistent_customer_fails(db):
    """Test adding credit for a non-existent customer raises CustomerNotFoundError."""
    with pytest.raises(ledger.CustomerNotFoundError, match="does not exist"):
        ledger.record_credit(db, customer_id=99999, amount_paise=10000)


def test_add_credit_invalid_amount_fails(db, sample_customer):
    """Test adding credit with zero or negative amount raises InvalidTransactionError."""
    with pytest.raises(ledger.InvalidTransactionError, match="greater than zero"):
        ledger.record_credit(db, customer_id=sample_customer.customer_id, amount_paise=0)

    with pytest.raises(ledger.InvalidTransactionError, match="greater than zero"):
        ledger.record_credit(db, customer_id=sample_customer.customer_id, amount_paise=-500)


def test_record_payment_success(db, sample_customer):
    """Test recording payment with valid payment method (CASH, UPI, OTHER)."""
    # First add credit
    ledger.record_credit(db, customer_id=sample_customer.customer_id, amount_paise=50000)

    # Record payment via UPI
    pay_tx = ledger.record_payment(
        db,
        customer_id=sample_customer.customer_id,
        amount_paise=20000,
        payment_method="UPI",
        item="PhonePe QR scan",
    )
    assert pay_tx.transaction_id is not None
    assert pay_tx.type == "PAYMENT"
    assert pay_tx.amount_paise == 20000
    assert pay_tx.payment_method == "UPI"
    assert pay_tx.status == "COMMITTED"

    # Balance should now be 50,000 - 20,000 = 30,000 paise (₹300)
    bal = ledger.get_customer_balance(db, sample_customer.customer_id)
    assert bal["balance_paise"] == 30000
    assert bal["balance_rupees"] == 300.0
    assert bal["total_credit_paise"] == 50000
    assert bal["total_payment_paise"] == 20000


def test_record_payment_invalid_method_fails(db, sample_customer):
    """Test recording payment with invalid payment method raises InvalidTransactionError."""
    with pytest.raises(ledger.InvalidTransactionError, match="Invalid payment method"):
        ledger.record_payment(
            db,
            customer_id=sample_customer.customer_id,
            amount_paise=10000,
            payment_method="BITCOIN",
        )


def test_outstanding_balance_formula(db, sample_customer):
    """
    Test authoritative balance formula:
    Credit ₹500 + Payment ₹200 + Credit ₹100 = Balance ₹400.
    """
    cid = sample_customer.customer_id
    ledger.record_credit(db, customer_id=cid, amount_paise=50000)  # +500
    ledger.record_payment(db, customer_id=cid, amount_paise=20000, payment_method="CASH")  # -200
    ledger.record_credit(db, customer_id=cid, amount_paise=10000)  # +100

    bal = ledger.get_customer_balance(db, cid)
    assert bal["balance_paise"] == 40000  # ₹400
    assert bal["balance_rupees"] == 400.0
    assert bal["transaction_count"] == 3


def test_transaction_history_preserves_chronological_entries(db, sample_customer):
    """Test retrieving customer transactions preserves all historical details."""
    cid = sample_customer.customer_id
    t1 = ledger.record_credit(db, customer_id=cid, amount_paise=30000, item="Item 1")
    t2 = ledger.record_payment(db, customer_id=cid, amount_paise=10000, payment_method="UPI")

    history = ledger.get_customer_transactions(db, cid)
    assert len(history) == 2
    # Ordered by most recent first
    assert history[0].transaction_id == t2.transaction_id
    assert history[1].transaction_id == t1.transaction_id


def test_idempotency_same_key_same_payload_returns_existing(db, sample_customer):
    """
    IDEMPOTENCY:
    Replaying request with the same idempotency key and identical payload returns
    the existing transaction without creating duplicate records.
    """
    cid = sample_customer.customer_id
    key = "voice-req-credit-999"

    tx1 = ledger.record_credit(
        db, customer_id=cid, amount_paise=34000, item="Sugar 5kg", idempotency_key=key
    )
    # Re-submit exact same request
    tx2 = ledger.record_credit(
        db, customer_id=cid, amount_paise=34000, item="Sugar 5kg", idempotency_key=key
    )

    assert tx1.transaction_id == tx2.transaction_id
    assert tx1.amount_paise == tx2.amount_paise

    # Ensure only 1 record exists in the database
    history = ledger.get_customer_transactions(db, cid)
    assert len(history) == 1


def test_idempotency_same_key_different_payload_conflicts(db, sample_customer):
    """
    IDEMPOTENCY:
    Reusing the same idempotency key with different data raises IdempotencyConflictError.
    """
    cid = sample_customer.customer_id
    key = "voice-req-credit-999"

    ledger.record_credit(
        db, customer_id=cid, amount_paise=34000, item="Sugar 5kg", idempotency_key=key
    )

    with pytest.raises(ledger.IdempotencyConflictError, match="different transaction payload"):
        ledger.record_credit(
            db, customer_id=cid, amount_paise=43000, item="Cooking Oil", idempotency_key=key
        )


def test_safe_additive_reversal_and_balance_verification(db, sample_customer):
    """
    SAFE REVERSALS:
    1. Original transaction preserved with status REVERSED.
    2. Compensating entry created with opposite type and status COMMITTED.
    3. Duplicate reversal rejected.
    4. Balance verified after reversal.
    5. Corrected entry recorded separately.
    """
    cid = sample_customer.customer_id

    # 1. Original Credit of ₹500
    orig = ledger.record_credit(db, customer_id=cid, amount_paise=50000, item="Initial entry")
    assert ledger.get_customer_balance(db, cid)["balance_paise"] == 50000

    # 2. Reverse original entry
    rev_result = ledger.reverse_transaction(
        db, transaction_id=orig.transaction_id, reason="Customer correction"
    )
    assert rev_result["reversed_transaction"]["status"] == "REVERSED"
    assert rev_result["compensating_transaction"]["type"] == "PAYMENT"
    assert rev_result["compensating_transaction"]["amount_paise"] == 50000
    assert rev_result["compensating_transaction"]["reversed_transaction_id"] == orig.transaction_id
    assert rev_result["balance_paise"] == 0

    # 3. Duplicate reversal replay should return existing reversal idempotently
    replay = ledger.reverse_transaction(db, transaction_id=orig.transaction_id)
    assert replay["compensating_transaction"]["transaction_id"] == rev_result["compensating_transaction"]["transaction_id"]

    # 4. Record corrected transaction of ₹300 separately
    corrected = ledger.record_credit(db, customer_id=cid, amount_paise=30000, item="Corrected entry")
    assert corrected.amount_paise == 30000

    # Final balance: 50,000 - 50,000 + 30,000 = 30,000 paise (₹300)
    final_bal = ledger.get_customer_balance(db, cid)
    assert final_bal["balance_paise"] == 30000
    assert final_bal["balance_rupees"] == 300.0


def test_cannot_reverse_a_reversal_entry(db, sample_customer):
    """Verify that a compensating reversal entry itself cannot be reversed."""
    cid = sample_customer.customer_id
    orig = ledger.record_credit(db, customer_id=cid, amount_paise=25000)
    rev_res = ledger.reverse_transaction(db, orig.transaction_id)
    comp_id = rev_res["compensating_transaction"]["transaction_id"]

    with pytest.raises(ledger.InvalidTransactionError, match="cannot be reversed"):
        ledger.reverse_transaction(db, comp_id)


def test_persistence_across_database_reconnection():
    """
    DATABASE RECONNECTION PERSISTENCE:
    Verify that credit, payment, and balance persist accurately across database connection closes.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        # Step 1: Open connection, initialize schema, insert customer and transactions
        conn1 = get_db_connection(db_path)
        conn1.executescript(SCHEMA_SQL)
        run_migrations(conn1)

        cust = customer_service.create_customer(conn1, CustomerCreate(name="Lakshmi", alias="Store"))
        cid = cust.customer_id
        ledger.record_credit(conn1, customer_id=cid, amount_paise=75000, item="Silk Saree")
        ledger.record_payment(conn1, customer_id=cid, amount_paise=25000, payment_method="CASH")
        conn1.close()

        # Step 2: Open completely new connection to the same SQLite file
        conn2 = get_db_connection(db_path)
        bal = ledger.get_customer_balance(conn2, cid)
        assert bal["balance_paise"] == 50000  # 750 - 250 = 500 rupees
        assert bal["balance_rupees"] == 500.0

        history = ledger.get_customer_transactions(conn2, cid)
        assert len(history) == 2
        conn2.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
