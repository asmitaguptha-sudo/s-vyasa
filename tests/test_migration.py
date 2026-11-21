"""
Tests for safe SQLite database schema migrations from legacy schemas.
Verifies that customer records are preserved, aliases migrated, and new columns added safely.
"""

import sqlite3
import pytest
from backend.database import get_db_connection, run_migrations
import backend.customer_service as customer_service

# Legacy database schema (version 1: prior to customer_aliases and reversal columns)
LEGACY_SCHEMA_V1 = """
PRAGMA foreign_keys = ON;

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    alias TEXT,
    phone TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('CREDIT', 'PAYMENT')),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    item TEXT,
    payment_method TEXT CHECK (payment_method IN ('CASH', 'UPI', 'OTHER') OR payment_method IS NULL),
    transaction_date TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    status TEXT NOT NULL DEFAULT 'COMMITTED' CHECK (status IN ('COMMITTED', 'REVERSED')),
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);
"""


@pytest.fixture
def legacy_db():
    """Create a database populated using the legacy schema and sample data."""
    conn = get_db_connection(":memory:")
    conn.executescript(LEGACY_SCHEMA_V1)

    # Insert legacy customer with old 'alias' column populated
    conn.execute(
        "INSERT INTO customers (name, alias, phone) VALUES ('Ramesh', 'Auto wale', '9876543210')"
    )
    cust_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Insert legacy transaction
    conn.execute(
        """
        INSERT INTO transactions (customer_id, type, amount_paise, status)
        VALUES (?, 'CREDIT', 50000, 'COMMITTED')
        """,
        (cust_id,),
    )
    conn.commit()
    yield conn
    conn.close()


def test_upgrade_legacy_database_preserves_records_and_migrates_aliases(legacy_db):
    """
    DATABASE MIGRATION TEST:
    Verify that upgrading a legacy database preserves existing customer and transaction data,
    migrates the old alias column into customer_aliases, and adds new columns safely.
    """
    # Run safe migrations on the legacy database
    run_migrations(legacy_db)
    legacy_db.current_user_id = 1  # Scope post-migration reads to the legacy merchant

    # 1. Verify customer record is preserved
    cursor = legacy_db.cursor()
    cursor.execute("SELECT * FROM customers WHERE name = 'Ramesh'")
    cust_row = cursor.fetchone()
    assert cust_row is not None
    assert cust_row["name"] == "Ramesh"
    assert cust_row["phone"] == "9876543210"

    # 2. Verify old alias was automatically migrated to customer_aliases table
    cursor.execute(
        "SELECT alias FROM customer_aliases WHERE customer_id = ?",
        (cust_row["customer_id"],),
    )
    alias_rows = cursor.fetchall()
    assert len(alias_rows) == 1
    assert alias_rows[0]["alias"] == "Auto wale"

    # 3. Verify customer_service reads the migrated customer seamlessly
    customer = customer_service.get_customer_by_id(legacy_db, cust_row["customer_id"])
    assert customer is not None
    assert "Auto wale" in customer.aliases

    # 4. Verify new columns (reversed_transaction_id, idempotency_key) now exist in transactions
    tx_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(transactions)").fetchall()}
    assert "reversed_transaction_id" in tx_cols
    assert "idempotency_key" in tx_cols

    # 5. Verify the existing transaction is preserved
    cursor.execute("SELECT * FROM transactions WHERE customer_id = ?", (cust_row["customer_id"],))
    tx_row = cursor.fetchone()
    assert tx_row is not None
    assert tx_row["amount_paise"] == 50000
    assert tx_row["type"] == "CREDIT"
    assert tx_row["reversed_transaction_id"] is None

    # 6. Verify reversal uniqueness constraint works on the migrated table
    # Add compensating reversal
    cursor.execute(
        """
        INSERT INTO transactions (user_id, customer_id, type, amount_paise, status, reversed_transaction_id)
        VALUES (1, ?, 'PAYMENT', 50000, 'COMMITTED', ?)
        """,
        (cust_row["customer_id"], tx_row["transaction_id"]),
    )
    legacy_db.commit()

    # Attempt second reversal on the same transaction -> should be blocked by unique index
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        cursor.execute(
            """
            INSERT INTO transactions (user_id, customer_id, type, amount_paise, status, reversed_transaction_id)
            VALUES (1, ?, 'PAYMENT', 50000, 'COMMITTED', ?)
            """,
            (cust_row["customer_id"], tx_row["transaction_id"]),
        )
        legacy_db.commit()
