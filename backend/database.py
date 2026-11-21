"""
Database connection, schema initialization, and versioned schema migration for Vaada Khata.
Supports multi-merchant account isolation with composite foreign keys, WAL mode,
foreign key enforcement, and atomic write transactions.
"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
from typing import Generator, List, Optional, Set, Tuple

from backend.config import DATABASE_PATH, LEGACY_CLAIM_SECRET, LEGACY_OWNER_PHONE

SCHEMA_SQL = """
-- Enable foreign key support
PRAGMA foreign_keys = ON;

-- 0. Schema migrations tracking table
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 1. Users / Merchants table: independent store owners
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    shop_name TEXT NOT NULL,
    voice_enrolled BOOLEAN NOT NULL DEFAULT 0,
    claim_token_hash TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUSPENDED', 'BOOTSTRAP_LOCKED')),
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Ensure primary merchant user 1 exists for default scoping
INSERT OR IGNORE INTO users (user_id, phone, name, shop_name, status)
VALUES (1, 'LEGACY_OWNER_1', 'Primary Merchant', 'Kirana Store', 'ACTIVE');

-- 2. Customers table: scoped by user_id with composite identity
CREATE TABLE IF NOT EXISTS customers (
    customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    phone TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
    UNIQUE (user_id, customer_id)
);

-- 3. Customer aliases table: scoped with composite foreign key to customers
CREATE TABLE IF NOT EXISTS customer_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, customer_id) REFERENCES customers(user_id, customer_id) ON DELETE CASCADE,
    UNIQUE (user_id, customer_id, alias COLLATE NOCASE)
);

-- 4. Transactions table: committed credit (udhaar) and payment ledger records
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
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
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id, customer_id) REFERENCES customers(user_id, customer_id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id, reversed_transaction_id) REFERENCES transactions(user_id, transaction_id) ON DELETE RESTRICT,
    UNIQUE (user_id, transaction_id)
);

-- 5. Promises table: future payment commitments tracked separately from actual payments
CREATE TABLE IF NOT EXISTS promises (
    promise_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'FULFILLED', 'CANCELLED', 'OVERDUE')),
    fulfilled_by_transaction_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id, customer_id) REFERENCES customers(user_id, customer_id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id, fulfilled_by_transaction_id) REFERENCES transactions(user_id, transaction_id) ON DELETE RESTRICT
);

-- 6. Pending transaction proposals table: stores AI proposals awaiting merchant confirmation
CREATE TABLE IF NOT EXISTS pending_proposals (
    proposal_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    intent TEXT NOT NULL CHECK (intent IN ('add_credit', 'record_payment', 'credit', 'payment')),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    item TEXT,
    payment_method TEXT CHECK (payment_method IN ('CASH', 'UPI', 'OTHER') OR payment_method IS NULL),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONFIRMED', 'CANCELLED')),
    committed_transaction_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id, customer_id) REFERENCES customers(user_id, customer_id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id, committed_transaction_id) REFERENCES transactions(user_id, transaction_id) ON DELETE RESTRICT,
    UNIQUE (user_id, proposal_id)
);

-- 7. Pending customer proposals table (Varshini's separate customer creation workflow)
CREATE TABLE IF NOT EXISTS pending_customer_proposals (
    proposal_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    alias TEXT,
    phone TEXT,
    idempotency_key TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONFIRMED', 'CANCELLED', 'EXPIRED')),
    created_customer_id INTEGER,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id, created_customer_id) REFERENCES customers(user_id, customer_id) ON DELETE RESTRICT,
    UNIQUE (user_id, proposal_id)
);

-- 8. Sessions table for server-side token authentication
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    auth_method TEXT NOT NULL CHECK (auth_method IN ('VOICE', 'PIN', 'PASSWORD')),
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- 9. Voice identities table for external biometric provider mapping
CREATE TABLE IF NOT EXISTS voice_identities (
    enrollment_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE,
    external_speaker_id TEXT NOT NULL UNIQUE,
    provider_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'ACTIVE', 'REVOKED')),
    enrolled_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Performance and lookup composite indexes
CREATE INDEX IF NOT EXISTS idx_customers_name ON customers (user_id, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_customer_aliases_alias ON customer_aliases (user_id, alias COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_customer_aliases_customer_id ON customer_aliases (user_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_transactions_customer_id ON transactions (user_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (user_id, transaction_date);
CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_reversed ON transactions (user_id, reversed_transaction_id) WHERE reversed_transaction_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_idempotency ON transactions (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_promises_customer_id ON promises (user_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_promises_due_date ON promises (user_id, due_date);
CREATE INDEX IF NOT EXISTS idx_proposals_customer ON pending_proposals (user_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON pending_proposals (user_id, status);
CREATE INDEX IF NOT EXISTS idx_cust_proposals_status ON pending_customer_proposals (user_id, status);
CREATE INDEX IF NOT EXISTS idx_cust_proposals_name ON pending_customer_proposals (user_id, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_cust_proposals_phone ON pending_customer_proposals (user_id, phone) WHERE phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cust_proposals_expiry ON pending_customer_proposals (status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cust_proposals_idempotency ON pending_customer_proposals (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions (token_hash, revoked_at, expires_at);
"""


class VaadaConnection(sqlite3.Connection):
    """Custom SQLite connection subclass that supports merchant ownership scoping."""
    current_user_id: Optional[int] = None


def get_db_connection(db_path: Optional[str] = None) -> VaadaConnection:
    """
    Establish and return a configured SQLite connection.
    Enables foreign keys, row factory for dict-like access, WAL journal mode,
    PRAGMA busy_timeout = 5000, and isolation_level=None for explicit atomic write locks.
    """
    path = db_path if db_path is not None else DATABASE_PATH
    conn = sqlite3.connect(path, factory=VaadaConnection, check_same_thread=False, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.current_user_id = None
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
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


def get_applied_migrations(conn: sqlite3.Connection) -> Set[int]:
    """Return set of migration version numbers already applied."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
    """)
    rows = cursor.execute("SELECT version FROM schema_migrations;").fetchall()
    return {r["version"] for r in rows}


def create_backup_file(db_path: str) -> Optional[str]:
    """Create a verified file backup of an existing physical database before migration."""
    if not db_path or db_path == ":memory:":
        return None
    p = Path(db_path)
    if not p.is_file():
        return None
    bak_path = str(p.with_suffix(".db.bak"))
    shutil.copy2(db_path, bak_path)
    return bak_path


def restore_backup_file(backup_path: str, target_path: str) -> None:
    """Restore database from backup file upon migration failure."""
    if backup_path and os.path.exists(backup_path) and target_path and target_path != ":memory:":
        shutil.copy2(backup_path, target_path)


def run_migrations(conn: sqlite3.Connection, db_path: Optional[str] = None) -> None:
    """
    Executes versioned migrations safely.
    Handles PRAGMA foreign_keys outside the transaction and performs
    clean table rebuilds with PRAGMA foreign_key_check.
    Rolls back automatically on failure without partial or corrupted state.
    """
    applied = get_applied_migrations(conn)

    # If Version 2 (multi-merchant composite schema) is already applied, nothing to do
    if 2 in applied:
        return

    # Check if this is a fresh database (no customers table exists yet)
    cursor = conn.cursor()
    has_customers = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='customers';"
    ).fetchone()

    if not has_customers:
        # Fresh database: SCHEMA_SQL creates the tables with composite foreign keys directly.
        # Ensure initial default merchant exists in users table
        cursor.execute("SELECT COUNT(*) as count FROM users;")
        if cursor.fetchone()["count"] == 0:
            cursor.execute("""
                INSERT INTO users (user_id, phone, name, shop_name, status)
                VALUES (1, 'LEGACY_OWNER_1', 'Primary Merchant', 'Kirana Store', 'ACTIVE');
            """)
        cursor.execute("INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (1, 'v1_baseline');")
        cursor.execute("INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (2, 'v2_multi_merchant_composite');")
        return

    # Existing database: inspect if customers already has user_id and composite constraint
    cust_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(customers)").fetchall()}
    if "user_id" in cust_cols and 2 in applied:
        return

    # -------------------------------------------------------------------------
    # Version 2 Migration: Table-Rebuild Migration to Composite Multi-Merchant Schema
    # -------------------------------------------------------------------------
    bak_path = create_backup_file(db_path) if db_path else None

    # Step 1: SQLite requires PRAGMA foreign_keys = OFF outside the transaction for table rebuilds
    conn.execute("PRAGMA foreign_keys = OFF;")

    try:
        conn.execute("BEGIN IMMEDIATE;")

        # 1. Ensure users table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                shop_name TEXT NOT NULL,
                voice_enrolled BOOLEAN NOT NULL DEFAULT 0,
                claim_token_hash TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUSPENDED', 'BOOTSTRAP_LOCKED')),
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
        """)

        # 2. Secure Legacy Owner Bootstrap
        user_count = conn.execute("SELECT COUNT(*) as cnt FROM users;").fetchone()["cnt"]
        if user_count == 0:
            cust_count = conn.execute("SELECT COUNT(*) as cnt FROM customers;").fetchone()["cnt"]
            if cust_count > 0:
                # Legacy records exist: bootstrap locked legacy owner with explicit claim token
                owner_phone = LEGACY_OWNER_PHONE or f"BOOTSTRAP_{secrets.token_hex(4).upper()}"
                if LEGACY_CLAIM_SECRET:
                    claim_hash = hashlib.sha256(LEGACY_CLAIM_SECRET.strip().encode()).hexdigest()
                else:
                    claim_hash = hashlib.sha256(secrets.token_hex(16).encode()).hexdigest()

                conn.execute("""
                    INSERT INTO users (user_id, phone, name, shop_name, status, claim_token_hash)
                    VALUES (1, ?, 'Legacy Store Owner', 'My Kirana Store', 'BOOTSTRAP_LOCKED', ?);
                """, (owner_phone, claim_hash))
            else:
                conn.execute("""
                    INSERT INTO users (user_id, phone, name, shop_name, status)
                    VALUES (1, 'LEGACY_OWNER_1', 'Primary Merchant', 'Kirana Store', 'ACTIVE');
                """)

        # 3. Build new v2 tables with composite foreign keys
        conn.execute("""
            CREATE TABLE customers_v2 (
                customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                phone TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
                UNIQUE (user_id, customer_id)
            );
        """)

        conn.execute("""
            CREATE TABLE customer_aliases_v2 (
                alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                alias TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (user_id, customer_id) REFERENCES customers(user_id, customer_id) ON DELETE CASCADE,
                UNIQUE (user_id, customer_id, alias COLLATE NOCASE)
            );
        """)

        conn.execute("""
            CREATE TABLE transactions_v2 (
                transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
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
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
                FOREIGN KEY (user_id, customer_id) REFERENCES customers(user_id, customer_id) ON DELETE RESTRICT,
                FOREIGN KEY (user_id, reversed_transaction_id) REFERENCES transactions_v2(user_id, transaction_id) ON DELETE RESTRICT,
                UNIQUE (user_id, transaction_id)
            );
        """)

        conn.execute("""
            CREATE TABLE promises_v2 (
                promise_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
                due_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'FULFILLED', 'CANCELLED', 'OVERDUE')),
                fulfilled_by_transaction_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
                FOREIGN KEY (user_id, customer_id) REFERENCES customers(user_id, customer_id) ON DELETE RESTRICT,
                FOREIGN KEY (user_id, fulfilled_by_transaction_id) REFERENCES transactions_v2(user_id, transaction_id) ON DELETE RESTRICT
            );
        """)

        conn.execute("""
            CREATE TABLE pending_proposals_v2 (
                proposal_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                intent TEXT NOT NULL CHECK (intent IN ('add_credit', 'record_payment', 'credit', 'payment')),
                amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
                item TEXT,
                payment_method TEXT CHECK (payment_method IN ('CASH', 'UPI', 'OTHER') OR payment_method IS NULL),
                status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONFIRMED', 'CANCELLED')),
                committed_transaction_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
                FOREIGN KEY (user_id, customer_id) REFERENCES customers(user_id, customer_id) ON DELETE RESTRICT,
                FOREIGN KEY (user_id, committed_transaction_id) REFERENCES transactions_v2(user_id, transaction_id) ON DELETE RESTRICT,
                UNIQUE (user_id, proposal_id)
            );
        """)

        # 4. Copy existing customer records (preserving IDs, timestamps, phone)
        conn.execute("""
            INSERT INTO customers_v2 (customer_id, user_id, name, phone, created_at)
            SELECT customer_id, 1, name, phone, created_at FROM customers;
        """)

        # 5. Copy customer aliases (preserving alias IDs, customer IDs, and legacy customer.alias column)
        has_aliases = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_aliases';"
        ).fetchone()
        if has_aliases:
            conn.execute("""
                INSERT OR IGNORE INTO customer_aliases_v2 (alias_id, user_id, customer_id, alias, created_at)
                SELECT alias_id, 1, customer_id, alias, created_at FROM customer_aliases;
            """)

        # Migrate legacy alias column from customers table if present
        if "alias" in cust_cols:
            conn.execute("""
                INSERT OR IGNORE INTO customer_aliases_v2 (user_id, customer_id, alias)
                SELECT 1, customer_id, trim(alias)
                FROM customers
                WHERE alias IS NOT NULL AND trim(alias) != '';
            """)

        # 6. Copy transactions (preserving transaction_id, status, timestamps, reversal links, idempotency)
        has_tx = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions';"
        ).fetchone()
        if has_tx:
            tx_cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
            rev_col = "reversed_transaction_id" if "reversed_transaction_id" in tx_cols else "NULL"
            idemp_col = "idempotency_key" if "idempotency_key" in tx_cols else "NULL"
            conn.execute(f"""
                INSERT INTO transactions_v2 (
                    transaction_id, user_id, customer_id, type, amount_paise, item,
                    payment_method, transaction_date, status, reversed_transaction_id,
                    idempotency_key, created_at
                )
                SELECT
                    transaction_id, 1, customer_id, type, amount_paise, item,
                    payment_method, transaction_date, status, {rev_col},
                    {idemp_col}, created_at
                FROM transactions;
            """)

        # 7. Copy promises (preserving promise_id, statuses, fulfillment links)
        has_promises = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='promises';"
        ).fetchone()
        if has_promises:
            conn.execute("""
                INSERT INTO promises_v2 (
                    promise_id, user_id, customer_id, amount_paise, due_date, status,
                    fulfilled_by_transaction_id, created_at
                )
                SELECT
                    promise_id, 1, customer_id, amount_paise, due_date, status,
                    fulfilled_by_transaction_id, created_at
                FROM promises;
            """)

        # 8. Copy pending proposals (preserving proposal_id, status, intent, amount)
        has_proposals = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_proposals';"
        ).fetchone()
        if has_proposals:
            conn.execute("""
                INSERT INTO pending_proposals_v2 (
                    proposal_id, user_id, customer_id, intent, amount_paise, item,
                    payment_method, status, committed_transaction_id, created_at
                )
                SELECT
                    proposal_id, 1, customer_id, intent, amount_paise, item,
                    payment_method, status, committed_transaction_id, created_at
                FROM pending_proposals;
            """)

        # 9. Drop legacy tables
        if has_aliases:
            conn.execute("DROP TABLE customer_aliases;")
        if has_tx:
            conn.execute("DROP TABLE transactions;")
        if has_promises:
            conn.execute("DROP TABLE promises;")
        if has_proposals:
            conn.execute("DROP TABLE pending_proposals;")
        conn.execute("DROP TABLE customers;")

        # 10. Rename _v2 tables to canonical names
        conn.execute("ALTER TABLE customers_v2 RENAME TO customers;")
        conn.execute("ALTER TABLE customer_aliases_v2 RENAME TO customer_aliases;")
        conn.execute("ALTER TABLE transactions_v2 RENAME TO transactions;")
        conn.execute("ALTER TABLE promises_v2 RENAME TO promises;")
        conn.execute("ALTER TABLE pending_proposals_v2 RENAME TO pending_proposals;")

        # 11. Create supporting tables if not existing
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_customer_proposals (
                proposal_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                alias TEXT,
                phone TEXT,
                idempotency_key TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONFIRMED', 'CANCELLED', 'EXPIRED')),
                created_customer_id INTEGER,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT,
                FOREIGN KEY (user_id, created_customer_id) REFERENCES customers(user_id, customer_id) ON DELETE RESTRICT,
                UNIQUE (user_id, proposal_id)
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                auth_method TEXT NOT NULL CHECK (auth_method IN ('VOICE', 'PIN', 'PASSWORD')),
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS voice_identities (
                enrollment_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE,
                external_speaker_id TEXT NOT NULL UNIQUE,
                provider_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('PENDING', 'ACTIVE', 'REVOKED')),
                enrolled_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
        """)

        # 12. Create composite indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_name ON customers (user_id, name COLLATE NOCASE);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customer_aliases_alias ON customer_aliases (user_id, alias COLLATE NOCASE);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customer_aliases_customer_id ON customer_aliases (user_id, customer_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_customer_id ON transactions (user_id, customer_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (user_id, transaction_date);")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_reversed ON transactions (user_id, reversed_transaction_id) WHERE reversed_transaction_id IS NOT NULL;")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_idempotency ON transactions (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_promises_customer_id ON promises (user_id, customer_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_promises_due_date ON promises (user_id, due_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_proposals_customer ON pending_proposals (user_id, customer_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_proposals_status ON pending_proposals (user_id, status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cust_proposals_status ON pending_customer_proposals (user_id, status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cust_proposals_name ON pending_customer_proposals (user_id, name COLLATE NOCASE);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cust_proposals_phone ON pending_customer_proposals (user_id, phone) WHERE phone IS NOT NULL;")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cust_proposals_expiry ON pending_customer_proposals (status, expires_at);")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_cust_proposals_idempotency ON pending_customer_proposals (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions (token_hash, revoked_at, expires_at);")

        # 13. Verify Foreign Key Integrity on Rebuilt Schema
        violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
        if violations:
            raise RuntimeError(f"Foreign key check failed after rebuild: {violations}")

        # 14. Record migration completion
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (1, 'v1_baseline');")
        conn.execute("INSERT INTO schema_migrations (version, name) VALUES (2, 'v2_multi_merchant_composite');")

        conn.execute("COMMIT;")
    except BaseException as exc:
        conn.execute("ROLLBACK;")
        if bak_path and db_path:
            restore_backup_file(bak_path, db_path)
        raise exc
    finally:
        # Re-enable foreign key enforcement outside transaction
        conn.execute("PRAGMA foreign_keys = ON;")
        if bak_path and os.path.exists(bak_path):
            try:
                os.remove(bak_path)
            except OSError:
                pass


def init_db(db_path: Optional[str] = None) -> None:
    """
    Initialize SQLite database tables, indexes, and apply any pending schema migrations.
    Safe to call multiple times.
    """
    conn = get_db_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        run_migrations(conn, db_path)
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
