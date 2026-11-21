"""
Tests for database initialization, schema integrity, and financial protection constraints.
"""

import sqlite3
import pytest
from backend.database import SCHEMA_SQL, get_db_connection


@pytest.fixture
def in_memory_db():
    """Create an isolated, in-memory SQLite database initialized with schema and migrations."""
    conn = get_db_connection(":memory:")
    from backend.database import SCHEMA_SQL, run_migrations
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)
    conn.commit()
    yield conn
    conn.close()


def test_schema_tables_exist(in_memory_db):
    """Verify customers, customer_aliases, transactions, and promises tables exist."""
    cursor = in_memory_db.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {row[0] for row in cursor.fetchall()}

    assert "customers" in tables
    assert "customer_aliases" in tables
    assert "transactions" in tables
    assert "promises" in tables


def test_schema_indexes_exist(in_memory_db):
    """Verify performance and lookup indexes exist."""
    cursor = in_memory_db.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index';")
    indexes = {row[0] for row in cursor.fetchall()}

    assert "idx_customers_name" in indexes
    assert "idx_customer_aliases_alias" in indexes
    assert "idx_customer_aliases_customer_id" in indexes
    assert "idx_transactions_customer_id" in indexes
    assert "uq_transactions_reversed" in indexes
    assert "uq_transactions_idempotency" in indexes
    assert "idx_promises_customer_id" in indexes


def test_customer_deletion_protection_with_transactions(in_memory_db):
    """
    FINANCIAL DATA PROTECTION:
    Verify that ON DELETE RESTRICT prevents deleting a customer who has financial transactions.
    """
    cursor = in_memory_db.cursor()
    cursor.execute("INSERT INTO customers (name) VALUES ('Ramesh')")
    customer_id = cursor.lastrowid

    cursor.execute(
        """
        INSERT INTO transactions (customer_id, type, amount_paise)
        VALUES (?, 'CREDIT', 30000)
        """,
        (customer_id,),
    )
    in_memory_db.commit()

    # Attempting to delete customer must be BLOCKED by foreign key RESTRICT constraint
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute("DELETE FROM customers WHERE customer_id = ?", (customer_id,))
        in_memory_db.commit()


def test_customer_deletion_protection_with_promises(in_memory_db):
    """
    FINANCIAL DATA PROTECTION:
    Verify that ON DELETE RESTRICT prevents deleting a customer who has recorded promises.
    """
    cursor = in_memory_db.cursor()
    cursor.execute("INSERT INTO customers (name) VALUES ('Suresh')")
    customer_id = cursor.lastrowid

    cursor.execute(
        """
        INSERT INTO promises (customer_id, amount_paise, due_date)
        VALUES (?, 50000, '2026-11-28')
        """,
        (customer_id,),
    )
    in_memory_db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute("DELETE FROM customers WHERE customer_id = ?", (customer_id,))
        in_memory_db.commit()


def test_customer_deletion_cascades_aliases_when_no_transactions(in_memory_db):
    """
    Verify that a customer with NO financial records can be deleted,
    and their aliases in customer_aliases are cleanly cascaded.
    """
    cursor = in_memory_db.cursor()
    cursor.execute("INSERT INTO customers (name) VALUES ('Ravi')")
    customer_id = cursor.lastrowid

    cursor.execute(
        "INSERT INTO customer_aliases (customer_id, alias) VALUES (?, 'Tea Shop')",
        (customer_id,),
    )
    in_memory_db.commit()

    # Deletion succeeds because no transactions or promises reference this customer
    cursor.execute("DELETE FROM customers WHERE customer_id = ?", (customer_id,))
    in_memory_db.commit()

    # Alias is cleanly removed
    cursor.execute("SELECT * FROM customer_aliases WHERE customer_id = ?", (customer_id,))
    assert cursor.fetchone() is None


def test_reversal_unique_constraint_prevents_duplicate_reversals(in_memory_db):
    """
    REVERSAL SAFETY:
    Verify that reversed_transaction_id UNIQUE prevents reversing the same transaction twice.
    """
    cursor = in_memory_db.cursor()
    cursor.execute("INSERT INTO customers (name) VALUES ('Ramesh')")
    customer_id = cursor.lastrowid

    # Original transaction (T1)
    cursor.execute(
        """
        INSERT INTO transactions (customer_id, type, amount_paise, status)
        VALUES (?, 'CREDIT', 34000, 'COMMITTED')
        """,
        (customer_id,),
    )
    t1_id = cursor.lastrowid
    in_memory_db.commit()

    # First compensating reversal entry (T2 pointing to T1)
    cursor.execute(
        """
        INSERT INTO transactions (customer_id, type, amount_paise, status, reversed_transaction_id)
        VALUES (?, 'PAYMENT', 34000, 'COMMITTED', ?)
        """,
        (customer_id, t1_id),
    )
    in_memory_db.commit()

    # Second attempt to reverse T1 must fail due to UNIQUE constraint on reversed_transaction_id
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        cursor.execute(
            """
            INSERT INTO transactions (customer_id, type, amount_paise, status, reversed_transaction_id)
            VALUES (?, 'PAYMENT', 34000, 'COMMITTED', ?)
            """,
            (customer_id, t1_id),
        )
        in_memory_db.commit()


def test_reversal_foreign_key_constraint(in_memory_db):
    """Verify that reversed_transaction_id must reference a valid transaction."""
    cursor = in_memory_db.cursor()
    cursor.execute("INSERT INTO customers (name) VALUES ('Ramesh')")
    customer_id = cursor.lastrowid

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute(
            """
            INSERT INTO transactions (customer_id, type, amount_paise, reversed_transaction_id)
            VALUES (?, 'PAYMENT', 34000, 999999)
            """,
            (customer_id,),
        )
        in_memory_db.commit()


def test_idempotency_key_uniqueness(in_memory_db):
    """Verify that idempotency_key prevents inserting duplicate transactions."""
    cursor = in_memory_db.cursor()
    cursor.execute("INSERT INTO customers (name) VALUES ('Ramesh')")
    customer_id = cursor.lastrowid

    cursor.execute(
        """
        INSERT INTO transactions (customer_id, type, amount_paise, idempotency_key)
        VALUES (?, 'CREDIT', 20000, 'voice-req-123')
        """,
        (customer_id,),
    )
    in_memory_db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        cursor.execute(
            """
            INSERT INTO transactions (customer_id, type, amount_paise, idempotency_key)
            VALUES (?, 'CREDIT', 20000, 'voice-req-123')
            """,
            (customer_id,),
        )
        in_memory_db.commit()


def test_transaction_type_constraint(in_memory_db):
    """Verify check constraint rejects invalid transaction types."""
    cursor = in_memory_db.cursor()
    cursor.execute("INSERT INTO customers (name) VALUES ('Test Customer')")
    customer_id = cursor.lastrowid

    with pytest.raises(sqlite3.IntegrityError):
        cursor.execute(
            """
            INSERT INTO transactions (customer_id, type, amount_paise)
            VALUES (?, 'INVALID_TYPE', 50000)
            """,
            (customer_id,),
        )
        in_memory_db.commit()


def test_transaction_amount_constraint(in_memory_db):
    """Verify check constraint requires amount_paise > 0."""
    cursor = in_memory_db.cursor()
    cursor.execute("INSERT INTO customers (name) VALUES ('Test Customer')")
    customer_id = cursor.lastrowid

    with pytest.raises(sqlite3.IntegrityError):
        cursor.execute(
            """
            INSERT INTO transactions (customer_id, type, amount_paise)
            VALUES (?, 'CREDIT', 0)
            """,
            (customer_id,),
        )
        in_memory_db.commit()
