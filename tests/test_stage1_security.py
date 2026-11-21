"""
Stage 1 Security Test Suite: Multi-Merchant Architecture and Constraints.
Covers:
1. Missing user_id rejection: unauthenticated operations are rejected with ValueError.
2. Cross-merchant isolation: queries and transactions cannot access or modify other merchants' data.
3. Cross-merchant engine constraints: composite foreign keys block cross-merchant references at SQLite level.
4. Merchant-scoped idempotency: identical keys are safely partitioned per merchant.
5. Pending customer proposals hardening: merchant idempotency index and unique constraints.
6. Safe deletion behaviour: ON DELETE RESTRICT preserves financial and customer history.
"""

import sqlite3
import pytest

from backend.database import SCHEMA_SQL, get_db_connection, run_migrations
import backend.customer_service as customer_service
import backend.ledger as ledger
from backend.schemas import CustomerCreate, CustomerLookupStatus


@pytest.fixture
def test_db():
    """Provides an isolated database with 2 distinct merchants (user_id=1 and user_id=2)."""
    conn = get_db_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)

    # Insert second merchant
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, phone, name, shop_name, status) "
        "VALUES (2, '9876500002', 'Second Merchant', 'General Store', 'ACTIVE');"
    )
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 1. Missing user_id Rejection Tests
# ---------------------------------------------------------------------------

def test_missing_user_id_rejected_on_customer_create(test_db):
    """Customer creation without explicit or connection-bound user_id is rejected."""
    test_db.current_user_id = None
    with pytest.raises(ValueError, match="Merchant user_id is required"):
        customer_service.create_customer(test_db, CustomerCreate(name="Orphan Customer"))


def test_missing_user_id_rejected_on_customer_lookup(test_db):
    """Customer lookup without user_id is rejected."""
    test_db.current_user_id = None
    with pytest.raises(ValueError, match="Merchant user_id is required"):
        customer_service.lookup_customer(test_db, query="Ramesh")


def test_missing_user_id_rejected_on_customer_get_by_id(test_db):
    """Customer lookup by ID without user_id is rejected."""
    test_db.current_user_id = None
    with pytest.raises(ValueError, match="Merchant user_id is required"):
        customer_service.get_customer_by_id(test_db, customer_id=1)


def test_missing_user_id_rejected_on_list_customers(test_db):
    """Listing customers without user_id is rejected."""
    test_db.current_user_id = None
    with pytest.raises(ValueError, match="Merchant user_id is required"):
        customer_service.list_customers(test_db)


def test_missing_user_id_rejected_on_ledger_operations(test_db):
    """Ledger credit, payment, reversal, and balance without user_id are rejected."""
    test_db.current_user_id = None
    with pytest.raises(ValueError, match="Merchant user_id is required"):
        ledger.record_credit(test_db, customer_id=1, amount_paise=10000)

    with pytest.raises(ValueError, match="Merchant user_id is required"):
        ledger.record_payment(test_db, customer_id=1, amount_paise=5000)

    with pytest.raises(ValueError, match="Merchant user_id is required"):
        ledger.get_customer_balance(test_db, customer_id=1)

    with pytest.raises(ValueError, match="Merchant user_id is required"):
        ledger.reverse_transaction(test_db, transaction_id=1)


# ---------------------------------------------------------------------------
# 2. Cross-Merchant Data Isolation Tests
# ---------------------------------------------------------------------------

def test_cross_merchant_customer_isolation(test_db):
    """Merchant 2 cannot read or look up Merchant 1's customers."""
    # Merchant 1 creates a customer
    c1 = customer_service.create_customer(test_db, CustomerCreate(name="Ramesh", alias="Auto wale"), user_id=1)

    # Merchant 2 looks up the customer by ID -> returns None
    assert customer_service.get_customer_by_id(test_db, c1.customer_id, user_id=2) is None

    # Merchant 2 searches by name -> NOT_FOUND
    res = customer_service.lookup_customer(test_db, query="Ramesh", user_id=2)
    assert res.status_code if hasattr(res, 'status_code') else res.status == CustomerLookupStatus.NOT_FOUND

    # Merchant 2 lists customers -> does not contain Merchant 1's customer
    m2_list = customer_service.list_customers(test_db, user_id=2)
    assert all(c.customer_id != c1.customer_id for c in m2_list)


def test_cross_merchant_ledger_isolation(test_db):
    """Merchant 2 cannot write transactions or view balance for Merchant 1's customer."""
    c1 = customer_service.create_customer(test_db, CustomerCreate(name="Suresh"), user_id=1)
    tx1 = ledger.record_credit(test_db, customer_id=c1.customer_id, amount_paise=50000, user_id=1)

    # Merchant 2 cannot record credit on Merchant 1's customer
    with pytest.raises(ledger.CustomerNotFoundError):
        ledger.record_credit(test_db, customer_id=c1.customer_id, amount_paise=10000, user_id=2)

    # Merchant 2 cannot get balance for Merchant 1's customer
    with pytest.raises(ledger.CustomerNotFoundError):
        ledger.get_customer_balance(test_db, customer_id=c1.customer_id, user_id=2)

    # Merchant 2 cannot reverse Merchant 1's transaction
    with pytest.raises(ledger.TransactionNotFoundError):
        ledger.reverse_transaction(test_db, transaction_id=tx1.transaction_id, user_id=2)


def test_cross_merchant_proposal_isolation(test_db):
    """Merchant 2 cannot view, confirm, or cancel Merchant 1's proposals."""
    c1 = customer_service.create_customer(test_db, CustomerCreate(name="Mahesh"), user_id=1)
    prop = ledger.create_pending_proposal(
        test_db,
        customer_id=c1.customer_id,
        intent="add_credit",
        amount_paise=25000,
        user_id=1,
    )

    # Merchant 2 cannot retrieve proposal
    assert ledger.get_pending_proposal(test_db, prop.proposal_id, user_id=2) is None

    # Merchant 2 cannot confirm proposal
    with pytest.raises(ledger.ProposalNotFoundError):
        ledger.confirm_pending_proposal(test_db, prop.proposal_id, user_id=2)

    # Merchant 2 cannot cancel proposal
    with pytest.raises(ledger.ProposalNotFoundError):
        ledger.cancel_pending_proposal(test_db, prop.proposal_id, user_id=2)


# ---------------------------------------------------------------------------
# 3. Composite Foreign Key Engine Constraints (SQLite-level)
# ---------------------------------------------------------------------------

def test_composite_foreign_key_cross_merchant_transactions_blocked(test_db):
    """Direct SQL insertion with mismatched user_id fails with FOREIGN KEY constraint error."""
    cursor = test_db.cursor()
    # Create customer for merchant 1
    cursor.execute("INSERT INTO customers (user_id, name) VALUES (1, 'Direct Ramesh');")
    c1_id = cursor.lastrowid
    test_db.commit()

    # Merchant 2 attempts to insert transaction for Merchant 1's customer
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute(
            "INSERT INTO transactions (user_id, customer_id, type, amount_paise) VALUES (2, ?, 'CREDIT', 1000);",
            (c1_id,),
        )
        test_db.commit()


def test_composite_foreign_key_cross_merchant_aliases_blocked(test_db):
    """Merchant 2 cannot insert alias referencing Merchant 1's customer."""
    cursor = test_db.cursor()
    cursor.execute("INSERT INTO customers (user_id, name) VALUES (1, 'Alias Target');")
    c1_id = cursor.lastrowid
    test_db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute(
            "INSERT INTO customer_aliases (user_id, customer_id, alias) VALUES (2, ?, 'Hacked Alias');",
            (c1_id,),
        )
        test_db.commit()


def test_composite_foreign_key_cross_merchant_promises_blocked(test_db):
    """Merchant 2 cannot attach promise referencing Merchant 1's customer."""
    cursor = test_db.cursor()
    cursor.execute("INSERT INTO customers (user_id, name) VALUES (1, 'Promise Target');")
    c1_id = cursor.lastrowid
    test_db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute(
            "INSERT INTO promises (user_id, customer_id, amount_paise, due_date) VALUES (2, ?, 50000, '2026-12-01');",
            (c1_id,),
        )
        test_db.commit()


def test_composite_foreign_key_cross_merchant_proposals_blocked(test_db):
    """Merchant 2 cannot create pending proposal referencing Merchant 1's customer."""
    cursor = test_db.cursor()
    cursor.execute("INSERT INTO customers (user_id, name) VALUES (1, 'Proposal Target');")
    c1_id = cursor.lastrowid
    test_db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute(
            "INSERT INTO pending_proposals (proposal_id, user_id, customer_id, intent, amount_paise) "
            "VALUES ('prop_x', 2, ?, 'add_credit', 10000);",
            (c1_id,),
        )
        test_db.commit()


# ---------------------------------------------------------------------------
# 4. Merchant-Scoped Idempotency
# ---------------------------------------------------------------------------

def test_merchant_scoped_idempotency_isolation(test_db):
    """Two different merchants can reuse the same idempotency key string without conflict."""
    c1 = customer_service.create_customer(test_db, CustomerCreate(name="M1 Customer"), user_id=1)
    c2 = customer_service.create_customer(test_db, CustomerCreate(name="M2 Customer"), user_id=2)

    shared_key = "pos-tx-common-idemp-001"

    # Both merchants record transactions with identical idempotency keys
    tx1 = ledger.record_credit(test_db, customer_id=c1.customer_id, amount_paise=10000, idempotency_key=shared_key, user_id=1)
    tx2 = ledger.record_credit(test_db, customer_id=c2.customer_id, amount_paise=20000, idempotency_key=shared_key, user_id=2)

    assert tx1.transaction_id != tx2.transaction_id
    assert tx1.user_id == 1
    assert tx2.user_id == 2
    assert tx1.amount_paise == 10000
    assert tx2.amount_paise == 20000

    # Merchant 1 repeating same key returns tx1
    tx1_replay = ledger.record_credit(test_db, customer_id=c1.customer_id, amount_paise=10000, idempotency_key=shared_key, user_id=1)
    assert tx1_replay.transaction_id == tx1.transaction_id


# ---------------------------------------------------------------------------
# 5. Pending Customer Proposals Hardening
# ---------------------------------------------------------------------------

def test_pending_customer_proposals_idempotency_and_constraints(test_db):
    """
    Verify pending_customer_proposals has:
    - Merchant-scoped idempotency key (uq_cust_proposals_idempotency)
    - Rejection of duplicate idempotency key for same merchant
    - Acceptance of identical idempotency key for different merchant
    """
    cursor = test_db.cursor()

    # Merchant 1 creates proposal with idempotency key
    cursor.execute("""
        INSERT INTO pending_customer_proposals (proposal_id, user_id, name, idempotency_key, expires_at)
        VALUES ('prop-c1', 1, 'Deepak', 'idemp-cust-1', '2026-12-01 10:00:00');
    """)
    test_db.commit()

    # Duplicate idempotency key for SAME merchant must FAIL
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        cursor.execute("""
            INSERT INTO pending_customer_proposals (proposal_id, user_id, name, idempotency_key, expires_at)
            VALUES ('prop-c2', 1, 'Deepak Duplicate', 'idemp-cust-1', '2026-12-01 10:00:00');
        """)
        test_db.commit()

    # SAME idempotency key for DIFFERENT merchant (user_id=2) must SUCCEED
    cursor.execute("""
        INSERT INTO pending_customer_proposals (proposal_id, user_id, name, idempotency_key, expires_at)
        VALUES ('prop-c3', 2, 'Deepak M2', 'idemp-cust-1', '2026-12-01 10:00:00');
    """)
    test_db.commit()

    cursor.execute("SELECT COUNT(*) as cnt FROM pending_customer_proposals WHERE idempotency_key = 'idemp-cust-1';")
    assert cursor.fetchone()["cnt"] == 2


# ---------------------------------------------------------------------------
# 6. Safe Deletion Behaviour (Financial History Preservation)
# ---------------------------------------------------------------------------

def test_on_delete_restrict_preserves_customer_with_transactions(test_db):
    """Deleting a customer who has financial transactions is blocked by RESTRICT."""
    c = customer_service.create_customer(test_db, CustomerCreate(name="Protected Customer"), user_id=1)
    ledger.record_credit(test_db, customer_id=c.customer_id, amount_paise=15000, user_id=1)

    cursor = test_db.cursor()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute("DELETE FROM customers WHERE customer_id = ?", (c.customer_id,))
        test_db.commit()


def test_on_delete_restrict_preserves_transaction_linked_to_promise(test_db):
    """Deleting a transaction that fulfilled a promise is blocked by RESTRICT."""
    cursor = test_db.cursor()
    cursor.execute("INSERT INTO customers (user_id, name) VALUES (1, 'Promise Holder');")
    cid = cursor.lastrowid

    cursor.execute("INSERT INTO transactions (user_id, customer_id, type, amount_paise) VALUES (1, ?, 'PAYMENT', 5000);", (cid,))
    tx_id = cursor.lastrowid

    cursor.execute("""
        INSERT INTO promises (user_id, customer_id, amount_paise, due_date, status, fulfilled_by_transaction_id)
        VALUES (1, ?, 5000, '2026-12-01', 'FULFILLED', ?);
    """, (cid, tx_id))
    test_db.commit()

    # Attempting to delete the fulfilling transaction must be BLOCKED by RESTRICT
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute("DELETE FROM transactions WHERE transaction_id = ?", (tx_id,))
        test_db.commit()


def test_on_delete_restrict_preserves_transaction_linked_to_proposal(test_db):
    """Deleting a transaction that committed a proposal is blocked by RESTRICT."""
    cursor = test_db.cursor()
    cursor.execute("INSERT INTO customers (user_id, name) VALUES (1, 'Proposal Holder');")
    cid = cursor.lastrowid

    cursor.execute("INSERT INTO transactions (user_id, customer_id, type, amount_paise) VALUES (1, ?, 'CREDIT', 7500);", (cid,))
    tx_id = cursor.lastrowid

    cursor.execute("""
        INSERT INTO pending_proposals (proposal_id, user_id, customer_id, intent, amount_paise, status, committed_transaction_id)
        VALUES ('prop_committed_01', 1, ?, 'add_credit', 7500, 'CONFIRMED', ?);
    """, (cid, tx_id))
    test_db.commit()

    # Attempting to delete the committing transaction must be BLOCKED by RESTRICT
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute("DELETE FROM transactions WHERE transaction_id = ?", (tx_id,))
        test_db.commit()


def test_on_delete_restrict_preserves_user_with_customers(test_db):
    """Deleting a user who owns customer records is blocked by RESTRICT."""
    customer_service.create_customer(test_db, CustomerCreate(name="Bound Customer"), user_id=1)
    cursor = test_db.cursor()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        cursor.execute("DELETE FROM users WHERE user_id = 1;")
        test_db.commit()
