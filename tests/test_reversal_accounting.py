"""
Tests verifying the Reversal Accounting balance calculation invariant:
Original credit ₹500 + reversal payment ₹500 + corrected credit ₹300 = outstanding ₹300.
Verifies complete audit trails and prevention of duplicate reversals.
"""

import sqlite3
import pytest
from backend.database import SCHEMA_SQL, get_db_connection


@pytest.fixture
def db():
    """Provides an isolated in-memory database with schema and migrations."""
    conn = get_db_connection(":memory:")
    from backend.database import SCHEMA_SQL, run_migrations
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)
    conn.commit()
    conn.current_user_id = 1
    yield conn
    conn.close()


def calculate_customer_balance(db: sqlite3.Connection, customer_id: int) -> int:
    """
    Deterministic ledger balance calculation:
    Outstanding balance (paise) = Total Credits - Total Payments.
    In an additive ledger, compensating reversals are recorded as payments offsetting original credits.
    """
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type = 'CREDIT' THEN amount_paise ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN type = 'PAYMENT' THEN amount_paise ELSE 0 END), 0) as balance_paise
        FROM transactions
        WHERE customer_id = ?
        """,
        (customer_id,),
    )
    row = cursor.fetchone()
    return row["balance_paise"] if row else 0


def test_reversal_accounting_balance_invariant_500_500_300(db):
    """
    VERIFY INVARIANT:
    Original credit ₹500 + reversal payment ₹500 + corrected credit ₹300 = outstanding ₹300.
    """
    cursor = db.cursor()
    cursor.execute("INSERT INTO customers (user_id, name) VALUES (1, 'Ramesh')")
    customer_id = cursor.lastrowid

    # -------------------------------------------------------------------------
    # Step 1: Original Credit of ₹500 (50,000 paise)
    # -------------------------------------------------------------------------
    cursor.execute(
        """
        INSERT INTO transactions (user_id, customer_id, type, amount_paise, status, item)
        VALUES (1, ?, 'CREDIT', 50000, 'COMMITTED', 'Groceries')
        """,
        (customer_id,),
    )
    t1_id = cursor.lastrowid
    db.commit()

    # Verify balance is ₹500 (50,000 paise)
    balance_step1 = calculate_customer_balance(db, customer_id)
    assert balance_step1 == 50000

    # -------------------------------------------------------------------------
    # Step 2: Merchant reverses T1 -> mark T1 REVERSED & append compensating PAYMENT ₹500
    # -------------------------------------------------------------------------
    cursor.execute(
        "UPDATE transactions SET status = 'REVERSED' WHERE transaction_id = ?",
        (t1_id,),
    )
    cursor.execute(
        """
        INSERT INTO transactions (user_id, customer_id, type, amount_paise, status, reversed_transaction_id, item)
        VALUES (1, ?, 'PAYMENT', 50000, 'COMMITTED', ?, 'Reversal of entry #500')
        """,
        (customer_id, t1_id),
    )
    t2_id = cursor.lastrowid
    db.commit()

    # Verify balance is now ₹0 (50,000 credit - 50,000 payment)
    balance_step2 = calculate_customer_balance(db, customer_id)
    assert balance_step2 == 0

    # -------------------------------------------------------------------------
    # Step 3: Record Corrected Credit of ₹300 (30,000 paise)
    # -------------------------------------------------------------------------
    cursor.execute(
        """
        INSERT INTO transactions (user_id, customer_id, type, amount_paise, status, item)
        VALUES (1, ?, 'CREDIT', 30000, 'COMMITTED', 'Corrected Groceries entry')
        """,
        (customer_id,),
    )
    t3_id = cursor.lastrowid
    db.commit()

    # Verify final balance is EXACTLY ₹300 (30,000 paise)
    # Math: (₹500 + ₹300 credits) - ₹500 payment = ₹300 outstanding
    final_balance = calculate_customer_balance(db, customer_id)
    assert final_balance == 30000

    # -------------------------------------------------------------------------
    # Audit Verification: All records remain in full detail
    # -------------------------------------------------------------------------
    cursor.execute(
        "SELECT transaction_id, type, amount_paise, status, reversed_transaction_id FROM transactions WHERE customer_id = ? ORDER BY transaction_id ASC",
        (customer_id,),
    )
    history = cursor.fetchall()
    assert len(history) == 3

    # T1: Original credit, marked REVERSED
    assert history[0]["transaction_id"] == t1_id
    assert history[0]["type"] == "CREDIT"
    assert history[0]["amount_paise"] == 50000
    assert history[0]["status"] == "REVERSED"

    # T2: Compensating payment, links to T1
    assert history[1]["transaction_id"] == t2_id
    assert history[1]["type"] == "PAYMENT"
    assert history[1]["amount_paise"] == 50000
    assert history[1]["reversed_transaction_id"] == t1_id

    # T3: Corrected credit
    assert history[2]["transaction_id"] == t3_id
    assert history[2]["type"] == "CREDIT"
    assert history[2]["amount_paise"] == 30000
    assert history[2]["status"] == "COMMITTED"

    # -------------------------------------------------------------------------
    # Reversal Protection: Duplicate reversal attempt on T1 MUST FAIL
    # -------------------------------------------------------------------------
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        cursor.execute(
            """
            INSERT INTO transactions (user_id, customer_id, type, amount_paise, status, reversed_transaction_id)
            VALUES (1, ?, 'PAYMENT', 50000, 'COMMITTED', ?)
            """,
            (customer_id, t1_id),
        )
        db.commit()
