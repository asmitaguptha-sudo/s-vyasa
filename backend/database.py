"""
Database connection, schema initialization, and safe schema migration for Vaada Khata.
Uses SQLite with WAL mode, foreign key constraints enabled, and RESTRICT deletion rules.
"""

from contextlib import contextmanager
import sqlite3
from typing import Generator, Optional
from backend.config import DATABASE_PATH

SCHEMA_SQL = """
-- Enable foreign key support
PRAGMA foreign_keys = ON;

-- Customers table: canonical records
CREATE TABLE IF NOT EXISTS customers (
    customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Customer aliases table: supports multiple shop-specific nicknames per customer
CREATE TABLE IF NOT EXISTS customer_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE CASCADE,
    UNIQUE (customer_id, alias COLLATE NOCASE)
);

-- Transactions table: committed credit (udhaar) and payment ledger records
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('CREDIT', 'PAYMENT')),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    item TEXT,
    payment_method TEXT CHECK (payment_method IN ('CASH', 'UPI', 'OTHER') OR payment_method IS NULL),
    transaction_date TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    status TEXT NOT NULL DEFAULT 'COMMITTED' CHECK (status IN ('COMMITTED', 'REVERSED')),
    reversed_transaction_id INTEGER,
    idempotency_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE RESTRICT,
    FOREIGN KEY (reversed_transaction_id) REFERENCES transactions(transaction_id) ON DELETE RESTRICT
);

-- Promises table: future payment commitments tracked separately from actual payments
CREATE TABLE IF NOT EXISTS promises (
    promise_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'FULFILLED', 'CANCELLED', 'OVERDUE')),
    fulfilled_by_transaction_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE RESTRICT,
    FOREIGN KEY (fulfilled_by_transaction_id) REFERENCES transactions(transaction_id) ON DELETE SET NULL
);

-- Pending proposals table: stores AI proposals awaiting explicit merchant confirmation
CREATE TABLE IF NOT EXISTS pending_proposals (
    proposal_id TEXT PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    intent TEXT NOT NULL CHECK (intent IN ('add_credit', 'record_payment', 'credit', 'payment')),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    item TEXT,
    payment_method TEXT CHECK (payment_method IN ('CASH', 'UPI', 'OTHER') OR payment_method IS NULL),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONFIRMED', 'CANCELLED')),
    committed_transaction_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE RESTRICT,
    FOREIGN KEY (committed_transaction_id) REFERENCES transactions(transaction_id) ON DELETE SET NULL
);

-- Performance and lookup indexes
CREATE INDEX IF NOT EXISTS idx_customers_name ON customers (name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_customer_aliases_alias ON customer_aliases (alias COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_customer_aliases_customer_id ON customer_aliases (customer_id);
CREATE INDEX IF NOT EXISTS idx_transactions_customer_id ON transactions (customer_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (transaction_date);
CREATE INDEX IF NOT EXISTS idx_promises_customer_id ON promises (customer_id);
CREATE INDEX IF NOT EXISTS idx_promises_due_date ON promises (due_date);
CREATE INDEX IF NOT EXISTS idx_proposals_customer ON pending_proposals (customer_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON pending_proposals (status);
"""


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Establish and return a configured SQLite connection.
    Enables foreign keys, row factory for dict-like access, WAL journal mode,
    and isolation_level=None to support explicit atomic(conn) write transactions.
    """
    path = db_path if db_path is not None else DATABASE_PATH
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
    return conn


@contextmanager
def atomic(conn: sqlite3.Connection) -> Generator[None, None, None]:
    """
    Acquire SQLite's write lock immediately using BEGIN IMMEDIATE.
    Re-entrant: if a transaction is already active on this connection,
    it yields safely without attempting a nested BEGIN.
    """
    if conn.in_transaction:
        yield
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def run_migrations(conn: sqlite3.Connection) -> None:
    """
    Safely upgrades an existing SQLite database schema without data loss.
    - Preserves existing customer records and migrates old alias column into customer_aliases.
    - Adds reversed_transaction_id and idempotency_key columns to transactions if missing.
    - Creates unique indexes for reversal protection and idempotency.
    - Creates pending_proposals table if missing.
    """
    # 1. Ensure customer_aliases table exists if upgrading an older database
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_aliases (
            alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE CASCADE,
            UNIQUE (customer_id, alias COLLATE NOCASE)
        );
    """)

    # 2. Migrate legacy alias column from customers table into customer_aliases table if present
    cust_cols = {r["name"] for r in conn.execute("PRAGMA table_info(customers)").fetchall()}
    if "alias" in cust_cols:
        conn.execute("""
            INSERT OR IGNORE INTO customer_aliases (customer_id, alias)
            SELECT customer_id, trim(alias)
            FROM customers
            WHERE alias IS NOT NULL AND trim(alias) != ''
        """)

    # 3. Add missing columns to transactions table if upgrading from legacy schema
    tx_cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "reversed_transaction_id" not in tx_cols:
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN reversed_transaction_id INTEGER REFERENCES transactions(transaction_id) ON DELETE RESTRICT"
        )
    if "idempotency_key" not in tx_cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN idempotency_key TEXT")

    # 4. Ensure pending_proposals table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_proposals (
            proposal_id TEXT PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            intent TEXT NOT NULL CHECK (intent IN ('add_credit', 'record_payment', 'credit', 'payment')),
            amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
            item TEXT,
            payment_method TEXT CHECK (payment_method IN ('CASH', 'UPI', 'OTHER') OR payment_method IS NULL),
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONFIRMED', 'CANCELLED')),
            committed_transaction_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE RESTRICT,
            FOREIGN KEY (committed_transaction_id) REFERENCES transactions(transaction_id) ON DELETE SET NULL
        );
    """)

    # 5. Ensure uniqueness indexes are created on the validated columns
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_reversed
        ON transactions (reversed_transaction_id)
        WHERE reversed_transaction_id IS NOT NULL;
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_idempotency
        ON transactions (idempotency_key)
        WHERE idempotency_key IS NOT NULL;
    """)


def init_db(db_path: Optional[str] = None) -> None:
    """
    Initialize SQLite database tables, indexes, and apply any schema migrations.
    Safe to call multiple times.
    """
    conn = get_db_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        run_migrations(conn)
    finally:
        conn.close()


def get_db() -> Generator[sqlite3.Connection, None, None]:
    """
    FastAPI dependency yielding an active SQLite database connection,
    closing it automatically after the request finishes.
    """
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()
