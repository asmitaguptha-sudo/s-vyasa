"""
Stage 1 Migration Tests: Versioned Table-Rebuild Migration to Composite Multi-Merchant Schema.
Verifies:
1. 100% preservation of customer IDs, transaction IDs, reversal links, idempotency keys,
   timestamps, transaction statuses, promise fulfillment links, and proposal states.
2. PRAGMA foreign_key_check passes with zero violations after rebuild.
3. Composite foreign key engine invariants: cross-merchant references are strictly rejected by SQLite.
4. Rollback safety: failed migrations roll back cleanly without partially renamed tables.
5. Secure legacy owner bootstrap: status is BOOTSTRAP_LOCKED and no hardcoded phone number.
"""

import os
from pathlib import Path
import sqlite3
import tempfile
import pytest

from backend.database import (
    SCHEMA_SQL,
    get_db_connection,
    init_db,
    run_migrations,
)


# Legacy Milestone 1/2 schema without user_id or composite foreign keys
LEGACY_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    alias TEXT,
    created_at TEXT NOT NULL DEFAULT '2026-11-20 10:00:00'
);

CREATE TABLE customer_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '2026-11-20 10:05:00',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE CASCADE,
    UNIQUE (customer_id, alias COLLATE NOCASE)
);

CREATE TABLE transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('CREDIT', 'PAYMENT')),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    item TEXT,
    payment_method TEXT,
    transaction_date TEXT NOT NULL DEFAULT '2026-11-20 11:00:00',
    status TEXT NOT NULL DEFAULT 'COMMITTED' CHECK (status IN ('COMMITTED', 'REVERSED')),
    reversed_transaction_id INTEGER,
    idempotency_key TEXT,
    created_at TEXT NOT NULL DEFAULT '2026-11-20 11:00:00',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE RESTRICT,
    FOREIGN KEY (reversed_transaction_id) REFERENCES transactions(transaction_id) ON DELETE RESTRICT
);

CREATE TABLE promises (
    promise_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'FULFILLED', 'CANCELLED', 'OVERDUE')),
    fulfilled_by_transaction_id INTEGER,
    created_at TEXT NOT NULL DEFAULT '2026-11-20 11:30:00',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE RESTRICT,
    FOREIGN KEY (fulfilled_by_transaction_id) REFERENCES transactions(transaction_id) ON DELETE SET NULL
);

CREATE TABLE pending_proposals (
    proposal_id TEXT PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    intent TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    item TEXT,
    payment_method TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    committed_transaction_id INTEGER,
    created_at TEXT NOT NULL DEFAULT '2026-11-20 12:00:00',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE RESTRICT,
    FOREIGN KEY (committed_transaction_id) REFERENCES transactions(transaction_id) ON DELETE SET NULL
);
"""


def test_fresh_database_initialization():
    """Verify fresh database initializes with version 2 and all composite schema tables."""
    conn = get_db_connection(":memory:")
    init_db(":memory:")
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)

    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {r[0] for r in cursor.fetchall()}

    expected_tables = {
        "schema_migrations", "users", "customers", "customer_aliases",
        "transactions", "promises", "pending_proposals", "pending_customer_proposals",
        "sessions", "voice_identities",
    }
    assert expected_tables.issubset(tables)

    # Check migration version
    cursor.execute("SELECT version FROM schema_migrations ORDER BY version ASC;")
    versions = [r[0] for r in cursor.fetchall()]
    assert 2 in versions
    conn.close()


def test_legacy_database_migration_preserves_all_records_and_links():
    """
    MIGRATION PRESERVATION TEST:
    Verifies that migrating a populated legacy Milestone 2 database preserves:
    - Customer IDs, phone numbers, and timestamps.
    - Multiple aliases per customer and legacy customer.alias.
    - Transaction IDs, types, amounts, dates, and statuses (COMMITTED & REVERSED).
    - Reversal links (reversed_transaction_id).
    - Idempotency keys.
    - Promise IDs, amounts, due dates, statuses, and fulfillment links.
    - Proposal IDs, intents, amounts, statuses, and committed transaction links.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        # 1. Create and populate legacy database
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.executescript(LEGACY_SCHEMA_SQL)
        cursor = conn.cursor()

        # Seed 2 customers (one with legacy alias column, one with separate aliases)
        cursor.execute("INSERT INTO customers (name, phone, alias, created_at) VALUES ('Ramesh', '9876511111', 'Auto wale', '2026-11-20 10:00:00');")
        c1_id = cursor.lastrowid
        cursor.execute("INSERT INTO customers (name, phone, alias, created_at) VALUES ('Suresh', '9876522222', NULL, '2026-11-20 10:10:00');")
        c2_id = cursor.lastrowid

        # Seed aliases in customer_aliases table
        cursor.execute("INSERT INTO customer_aliases (customer_id, alias, created_at) VALUES (?, 'School wale', '2026-11-20 10:15:00');", (c1_id,))
        a1_id = cursor.lastrowid
        cursor.execute("INSERT INTO customer_aliases (customer_id, alias, created_at) VALUES (?, 'Doodh wale', '2026-11-20 10:20:00');", (c2_id,))
        a2_id = cursor.lastrowid

        # Seed transactions: Credit, Payment, Reversal
        cursor.execute("""
            INSERT INTO transactions (customer_id, type, amount_paise, item, transaction_date, status, idempotency_key, created_at)
            VALUES (?, 'CREDIT', 50000, 'Rice Bag', '2026-11-20 11:00:00', 'REVERSED', 'idemp-001', '2026-11-20 11:00:00');
        """, (c1_id,))
        t1_id = cursor.lastrowid

        # Compensating payment linked to T1
        cursor.execute("""
            INSERT INTO transactions (customer_id, type, amount_paise, item, transaction_date, status, reversed_transaction_id, idempotency_key, created_at)
            VALUES (?, 'PAYMENT', 50000, 'Reversal of #1', '2026-11-20 11:05:00', 'COMMITTED', ?, 'idemp-rev-001', '2026-11-20 11:05:00');
        """, (c1_id, t1_id))
        t2_id = cursor.lastrowid

        # Normal payment for customer 2
        cursor.execute("""
            INSERT INTO transactions (customer_id, type, amount_paise, payment_method, transaction_date, status, created_at)
            VALUES (?, 'PAYMENT', 20000, 'UPI', '2026-11-20 11:15:00', 'COMMITTED', '2026-11-20 11:15:00');
        """, (c2_id,))
        t3_id = cursor.lastrowid

        # Seed promises (one fulfilled by T3)
        cursor.execute("""
            INSERT INTO promises (customer_id, amount_paise, due_date, status, fulfilled_by_transaction_id, created_at)
            VALUES (?, 20000, '2026-11-25', 'FULFILLED', ?, '2026-11-20 11:30:00');
        """, (c2_id, t3_id))
        p1_id = cursor.lastrowid

        # Seed pending proposals (one confirmed by T2)
        cursor.execute("""
            INSERT INTO pending_proposals (proposal_id, customer_id, intent, amount_paise, status, committed_transaction_id, created_at)
            VALUES ('prop-legacy-01', ?, 'add_credit', 50000, 'CONFIRMED', ?, '2026-11-20 12:00:00');
        """, (c1_id, t2_id))

        conn.close()

        # ---------------------------------------------------------------------
        # 2. Execute Version 2 Table-Rebuild Migration
        # ---------------------------------------------------------------------
        conn = get_db_connection(db_path)
        run_migrations(conn, db_path)

        # ---------------------------------------------------------------------
        # 3. Verify PRAGMA foreign_key_check passes with 0 violations
        # ---------------------------------------------------------------------
        violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
        assert len(violations) == 0, f"Foreign key check had violations: {violations}"

        cursor = conn.cursor()

        # 4. Verify Customers Preservation
        cursor.execute("SELECT * FROM customers ORDER BY customer_id ASC;")
        migrated_custs = cursor.fetchall()
        assert len(migrated_custs) == 2
        assert migrated_custs[0]["customer_id"] == c1_id
        assert migrated_custs[0]["user_id"] == 1
        assert migrated_custs[0]["name"] == "Ramesh"
        assert migrated_custs[0]["phone"] == "9876511111"
        assert migrated_custs[0]["created_at"] == "2026-11-20 10:00:00"

        assert migrated_custs[1]["customer_id"] == c2_id
        assert migrated_custs[1]["user_id"] == 1
        assert migrated_custs[1]["name"] == "Suresh"
        assert migrated_custs[1]["phone"] == "9876522222"

        # 5. Verify Aliases Preservation (including migrated legacy alias 'Auto wale')
        cursor.execute("SELECT * FROM customer_aliases WHERE customer_id = ? ORDER BY alias_id ASC;", (c1_id,))
        c1_aliases = {r["alias"] for r in cursor.fetchall()}
        assert "Auto wale" in c1_aliases
        assert "School wale" in c1_aliases

        # 6. Verify Transactions Preservation
        cursor.execute("SELECT * FROM transactions ORDER BY transaction_id ASC;")
        migrated_txs = cursor.fetchall()
        assert len(migrated_txs) == 3

        # T1: Original credit, REVERSED
        assert migrated_txs[0]["transaction_id"] == t1_id
        assert migrated_txs[0]["user_id"] == 1
        assert migrated_txs[0]["customer_id"] == c1_id
        assert migrated_txs[0]["type"] == "CREDIT"
        assert migrated_txs[0]["amount_paise"] == 50000
        assert migrated_txs[0]["status"] == "REVERSED"
        assert migrated_txs[0]["idempotency_key"] == "idemp-001"
        assert migrated_txs[0]["transaction_date"] == "2026-11-20 11:00:00"

        # T2: Compensating payment, COMMITTED, links to T1
        assert migrated_txs[1]["transaction_id"] == t2_id
        assert migrated_txs[1]["user_id"] == 1
        assert migrated_txs[1]["reversed_transaction_id"] == t1_id
        assert migrated_txs[1]["status"] == "COMMITTED"
        assert migrated_txs[1]["idempotency_key"] == "idemp-rev-001"

        # T3: Normal payment
        assert migrated_txs[2]["transaction_id"] == t3_id
        assert migrated_txs[2]["payment_method"] == "UPI"
        assert migrated_txs[2]["amount_paise"] == 20000

        # 7. Verify Promises Preservation
        cursor.execute("SELECT * FROM promises WHERE promise_id = ?;", (p1_id,))
        migrated_promise = cursor.fetchone()
        assert migrated_promise["promise_id"] == p1_id
        assert migrated_promise["user_id"] == 1
        assert migrated_promise["fulfilled_by_transaction_id"] == t3_id
        assert migrated_promise["status"] == "FULFILLED"
        assert migrated_promise["due_date"] == "2026-11-25"

        # 8. Verify Proposals Preservation
        cursor.execute("SELECT * FROM pending_proposals WHERE proposal_id = 'prop-legacy-01';")
        migrated_prop = cursor.fetchone()
        assert migrated_prop["proposal_id"] == "prop-legacy-01"
        assert migrated_prop["user_id"] == 1
        assert migrated_prop["customer_id"] == c1_id
        assert migrated_prop["status"] == "CONFIRMED"
        assert migrated_prop["committed_transaction_id"] == t2_id

        # 9. Verify Secure Legacy Owner Bootstrap
        cursor.execute("SELECT * FROM users WHERE user_id = 1;")
        owner = cursor.fetchone()
        assert owner is not None
        assert owner["status"] == "BOOTSTRAP_LOCKED"
        assert owner["phone"] != "9876543210"  # Verify not hardcoded dummy phone
        assert owner["claim_token_hash"] is not None

        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_composite_foreign_key_engine_enforcement():
    """
    ENGINE-LEVEL MULTI-TENANT ISOLATION:
    Verify SQLite rejects cross-merchant references via composite foreign keys.
    """
    conn = get_db_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)

    cursor = conn.cursor()
    # Ensure Merchant 1 and Merchant 2 exist
    cursor.execute("INSERT OR REPLACE INTO users (user_id, phone, name, shop_name) VALUES (1, '9000000001', 'M1', 'Shop 1');")
    cursor.execute("INSERT INTO users (user_id, phone, name, shop_name) VALUES (2, '9000000002', 'M2', 'Shop 2');")

    # Create Customer 10 for Merchant 1
    cursor.execute("INSERT INTO customers (customer_id, user_id, name) VALUES (10, 1, 'Ramesh');")

    # TEST 1: Merchant 2 cannot insert transaction referencing Merchant 1's Customer 10
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute("""
            INSERT INTO transactions (user_id, customer_id, type, amount_paise)
            VALUES (2, 10, 'CREDIT', 10000);
        """)

    # TEST 2: Merchant 1 successfully inserts Transaction 100 for Customer 10
    cursor.execute("""
        INSERT INTO transactions (transaction_id, user_id, customer_id, type, amount_paise)
        VALUES (100, 1, 10, 'CREDIT', 10000);
    """)

    # TEST 3: Merchant 2 cannot reverse Merchant 1's Transaction 100
    # Composite foreign key (user_id, reversed_transaction_id) requires user_id match
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute("""
            INSERT INTO transactions (user_id, customer_id, type, amount_paise, reversed_transaction_id)
            VALUES (2, 10, 'PAYMENT', 10000, 100);
        """)

    # TEST 4: Merchant 2 cannot add alias to Merchant 1's Customer 10
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute("""
            INSERT INTO customer_aliases (user_id, customer_id, alias)
            VALUES (2, 10, 'Malicious Alias');
        """)

    # TEST 5: Merchant 2 cannot attach promise to Merchant 1's Customer 10
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute("""
            INSERT INTO promises (user_id, customer_id, amount_paise, due_date)
            VALUES (2, 10, 10000, '2026-12-01');
        """)

    conn.close()


def test_migration_rollback_on_foreign_key_violation():
    """
    ROLLBACK SAFETY TEST:
    Verify that if a database has corrupted foreign keys prior to migration,
    the migration rolls back cleanly without leaving partially rebuilt tables.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.executescript(LEGACY_SCHEMA_SQL)
        cursor = conn.cursor()

        # Seed valid customer
        cursor.execute("INSERT INTO customers (customer_id, name) VALUES (1, 'Ramesh');")

        # Inject an orphan transaction with invalid customer_id=999 while foreign keys are off
        conn.execute("PRAGMA foreign_keys = OFF;")
        cursor.execute("""
            INSERT INTO transactions (transaction_id, customer_id, type, amount_paise)
            VALUES (1, 999, 'CREDIT', 5000);
        """)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.close()

        # Attempt migration: PRAGMA foreign_key_check MUST detect the orphan transaction and roll back
        conn = get_db_connection(db_path)
        with pytest.raises(RuntimeError, match="Foreign key check failed after rebuild"):
            run_migrations(conn, db_path)

        # Verify the database rolled back cleanly and still has legacy schema intact
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {r[0] for r in cursor.fetchall()}

        # Verify no _v2 temporary tables were left behind
        assert "customers_v2" not in tables
        assert "transactions_v2" not in tables
        assert "customers" in tables
        assert "transactions" in tables

        # Verify version 2 was NOT recorded in migrations
        cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations';")
        if cursor.fetchone():
            cursor.execute("SELECT version FROM schema_migrations WHERE version = 2;")
            assert cursor.fetchone() is None

        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
