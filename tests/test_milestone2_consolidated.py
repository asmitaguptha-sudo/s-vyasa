"""
Consolidated Milestone 2 tests for Vaada Khata:
1. AI contract strict 5 fields, item: null, extra fields rejected, numeric amounts, NaN/Inf/negative rejection.
2. Backend-controlled proposals, explicit confirmation, repeated confirmation idempotency, cancellation.
3. Ledger safety: integer paise, idempotency for credit/payment/reversal, additive reversals.
4. Database safety: atomic transactions, concurrent database access, integer constraints.
5. API safety: health check 503 on error, HTTP status codes, duplicate AI commits.
"""

from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
import pytest
from fastapi.testclient import TestClient

from backend.database import SCHEMA_SQL, get_db, get_db_connection, run_migrations
import backend.ledger as ledger
import backend.customer_service as customer_service
from backend.schemas import CustomerCreate
from backend.main import app


@pytest.fixture
def test_client():
    """Provides a TestClient with an isolated in-memory database overriding get_db."""
    conn = get_db_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)
    conn.current_user_id = 1

    def override_get_db():
        try:
            yield conn
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()
    conn.close()


# ---------------------------------------------------------------------------
# 1. AI Contract Tests: Exact 5 Fields, Extra Fields Forbidden, Item Null, Validation
# ---------------------------------------------------------------------------

def test_ai_contract_accepts_valid_five_fields_with_null_item(test_client):
    """Verify that a valid 5-field contract with item: null is accepted."""
    test_client.post("/customers", json={"name": "Ramesh"})
    payload = {
        "intent": "add_credit",
        "customer_name": "Ramesh",
        "amount": 300,
        "item": None,
        "confirmation_required": True,
    }
    res = test_client.post("/ai/contract/validate", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["contract_valid"] is True
    assert data["received_proposal"]["item"] is None
    assert data["amount_paise"] == 30000


def test_ai_contract_rejects_extra_fields(test_client):
    """EXTRA FIELDS FORBIDDEN: Passing any extra field must return 422 Unprocessable Entity."""
    payload = {
        "intent": "add_credit",
        "customer_name": "Ramesh",
        "amount": 300,
        "item": None,
        "confirmation_required": True,
        "unexpected_field": "malicious_payload",
    }
    res = test_client.post("/ai/contract/validate", json=payload)
    assert res.status_code == 422
    errors = res.json()["detail"]
    assert any("extra_forbidden" in str(e) for e in errors)


def test_ai_contract_rejects_missing_fields(test_client):
    """REQUIRED FIELDS: Omitting any of the five required fields must return 422."""
    # Missing item completely
    res1 = test_client.post(
        "/ai/contract/validate",
        json={"intent": "add_credit", "customer_name": "Ramesh", "amount": 300, "confirmation_required": True},
    )
    assert res1.status_code == 422

    # Missing confirmation_required
    res2 = test_client.post(
        "/ai/contract/validate",
        json={"intent": "add_credit", "customer_name": "Ramesh", "amount": 300, "item": None},
    )
    assert res2.status_code == 422


def test_ai_contract_rejects_nan_infinity_negative_and_boolean(test_client):
    """Verify strict rejection of NaN, infinity, negative amounts, booleans, and > 2 decimals."""
    base = {"intent": "add_credit", "customer_name": "Ramesh", "item": None, "confirmation_required": True}

    # Boolean amount
    res_bool = test_client.post("/ai/contract/validate", json={**base, "amount": True})
    assert res_bool.status_code == 422

    # Negative amount
    res_neg = test_client.post("/ai/contract/validate", json={**base, "amount": -100})
    assert res_neg.status_code == 422

    # NaN / Infinity
    res_nan = test_client.post("/ai/contract/validate", json={**base, "amount": "NaN"})
    assert res_nan.status_code == 422
    res_inf = test_client.post("/ai/contract/validate", json={**base, "amount": "Infinity"})
    assert res_inf.status_code == 422

    # Fractional paise (> 2 decimal places)
    res_frac = test_client.post("/ai/contract/validate", json={**base, "amount": 45.678})
    assert res_frac.status_code == 422


# ---------------------------------------------------------------------------
# 2. Confirmation Safety: Backend-Controlled Proposals & Explicit Confirmation
# ---------------------------------------------------------------------------

def test_backend_controlled_proposal_workflow(test_client):
    """
    CONFIRMATION SAFETY WORKFLOW:
    1. AI layer submits proposal via /ai/contract/propose.
    2. Backend creates pending proposal with proposal_id and confirmation prompt.
    3. Ledger is NOT committed yet (transactions table remains empty).
    4. Merchant confirms via /ai/proposals/{proposal_id}/confirm -> transaction commits.
    """
    c_res = test_client.post("/customers", json={"name": "Ramesh", "alias": "Auto wale"})
    cid = c_res.json()["customer_id"]

    # Step 1: Submit AI contract proposal
    proposal_payload = {
        "intent": "add_credit",
        "customer_name": "Ramesh",
        "amount": 340,
        "item": "Rice & Oil",
        "confirmation_required": True,
    }
    prop_res = test_client.post("/ai/contract/propose", json=proposal_payload)
    assert prop_res.status_code == 200
    prop_data = prop_res.json()
    assert prop_data["status"] == "PENDING_CONFIRMATION"
    assert prop_data["committed"] is False
    assert prop_data["amount_paise"] == 34000
    assert "Ramesh ke ₹340.00 add kar doon?" in prop_data["confirmation_prompt"]
    proposal_id = prop_data["proposal_id"]

    # Step 2: Verify transactions table is still empty (No premature commit!)
    bal_res = test_client.get(f"/customers/{cid}/balance")
    assert bal_res.json()["balance_paise"] == 0
    assert bal_res.json()["transaction_count"] == 0

    # Step 3: Merchant confirms ("Haan") -> execute explicit confirmation
    confirm_res = test_client.post(f"/ai/proposals/{proposal_id}/confirm")
    assert confirm_res.status_code == 200
    confirm_data = confirm_res.json()
    assert confirm_data["status"] == "CONFIRMED"
    assert confirm_data["committed"] is True
    assert confirm_data["transaction"]["amount_paise"] == 34000
    assert confirm_data["customer_balance"]["balance_paise"] == 34000


def test_repeated_confirmation_is_idempotent(test_client):
    """
    CONFIRMATION IDEMPOTENCY:
    Calling confirm repeatedly for the same proposal_id returns the existing
    transaction without creating duplicate ledger records.
    """
    test_client.post("/customers", json={"name": "Suresh"})
    prop_res = test_client.post(
        "/ai/contract/propose",
        json={"intent": "add_credit", "customer_name": "Suresh", "amount": 200, "item": None, "confirmation_required": True},
    )
    proposal_id = prop_res.json()["proposal_id"]

    # First confirmation
    res1 = test_client.post(f"/ai/proposals/{proposal_id}/confirm")
    assert res1.status_code == 200
    tx1_id = res1.json()["transaction"]["transaction_id"]

    # Repeated confirmation (e.g. voice retry or duplicate tap)
    res2 = test_client.post(f"/ai/proposals/{proposal_id}/confirm")
    assert res2.status_code == 200
    tx2_id = res2.json()["transaction"]["transaction_id"]

    # Must return exact same transaction ID
    assert tx1_id == tx2_id

    # Verify only 1 transaction exists
    cid = res1.json()["customer_balance"]["customer_id"]
    bal = test_client.get(f"/customers/{cid}/balance").json()
    assert bal["transaction_count"] == 1
    assert bal["balance_paise"] == 20000


def test_cancelled_proposal_cannot_be_confirmed(test_client):
    """
    PROPOSAL CANCELLATION:
    If merchant cancels ('Nahi, rehne do'), confirming the cancelled proposal returns 400.
    """
    test_client.post("/customers", json={"name": "Ramesh"})
    prop_res = test_client.post(
        "/ai/contract/propose",
        json={"intent": "add_credit", "customer_name": "Ramesh", "amount": 500, "item": None, "confirmation_required": True},
    )
    proposal_id = prop_res.json()["proposal_id"]

    # Merchant cancels
    cancel_res = test_client.post(f"/ai/proposals/{proposal_id}/cancel")
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == "CANCELLED"

    # Attempting to confirm cancelled proposal must be rejected
    confirm_res = test_client.post(f"/ai/proposals/{proposal_id}/confirm")
    assert confirm_res.status_code == 400
    assert "cancelled" in confirm_res.json()["detail"]


# ---------------------------------------------------------------------------
# 3. Ledger Safety: Idempotency Conflicts, Reversal Conflicts, Invalid Values
# ---------------------------------------------------------------------------

def test_idempotency_conflict_on_different_payload(test_client):
    """
    IDEMPOTENCY SAFETY:
    Reusing the same idempotency_key with different payload values returns 409 Conflict.
    """
    c_res = test_client.post("/customers", json={"name": "Ramesh"})
    cid = c_res.json()["customer_id"]

    key = "idem-test-key-001"
    # First request
    r1 = test_client.post(f"/customers/{cid}/credit", json={"amount": 100, "idempotency_key": key})
    assert r1.status_code == 201

    # Second request with SAME key but DIFFERENT amount -> 409 Conflict
    r2 = test_client.post(f"/customers/{cid}/credit", json={"amount": 200, "idempotency_key": key})
    assert r2.status_code == 409
    assert "different transaction payload" in r2.json()["detail"]


def test_reversal_conflict_prevents_double_reversal(test_client):
    """
    REVERSAL SAFETY:
    Reversing an already reversed transaction fails with 409 Conflict or returns idempotent replay.
    """
    c_res = test_client.post("/customers", json={"name": "Ramesh"})
    cid = c_res.json()["customer_id"]

    cr = test_client.post(f"/customers/{cid}/credit", json={"amount": 400})
    tx_id = cr.json()["transaction_id"]

    # First reversal
    rev1 = test_client.post(f"/transactions/{tx_id}/reverse", json={"reason": "Correction"})
    assert rev1.status_code == 200
    assert rev1.json()["reversed_transaction"]["status"] == "REVERSED"

    # Second reversal replay returns idempotent replay
    rev2 = test_client.post(f"/transactions/{tx_id}/reverse")
    assert rev2.status_code == 200
    assert "already reversed" in rev2.json()["message"]


def test_invalid_paise_values_rejected():
    """Verify that non-integer, bool, zero, or negative paise amounts are rejected."""
    conn = get_db_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)
    conn.current_user_id = 1

    cust = customer_service.create_customer(conn, CustomerCreate(name="TestCust"))
    cid = cust.customer_id

    with pytest.raises(ledger.InvalidTransactionError):
        ledger.record_credit(conn, customer_id=cid, amount_paise=0)

    with pytest.raises(ledger.InvalidTransactionError):
        ledger.record_credit(conn, customer_id=cid, amount_paise=-100)

    with pytest.raises(ledger.InvalidTransactionError):
        ledger.record_credit(conn, customer_id=cid, amount_paise=True)  # type: ignore

    with pytest.raises(ledger.InvalidTransactionError):
        ledger.record_credit(conn, customer_id=cid, amount_paise=12.5)  # type: ignore

    conn.close()


# ---------------------------------------------------------------------------
# 4. Database Safety: Concurrent Writes & Atomic Transactions
# ---------------------------------------------------------------------------

def test_concurrent_writes_thread_safety():
    """
    CONCURRENT WRITES & BALANCE INTEGRITY:
    Execute 20 concurrent credit operations across multiple threads against a disk SQLite file with WAL mode.
    Guarantees atomic execution via BEGIN IMMEDIATE without deadlocks, with exact balance arithmetic.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    init_conn = None
    verify_conn = None
    try:
        # Initialize database
        init_conn = get_db_connection(db_path)
        init_conn.executescript(SCHEMA_SQL)
        run_migrations(init_conn)
        init_conn.current_user_id = 1
        cust = customer_service.create_customer(init_conn, CustomerCreate(name="ConcurCust"))
        cid = cust.customer_id
        init_conn.close()
        init_conn = None

        num_threads = 10
        credits_per_thread = 2
        amount_each = 1000  # ₹10 (1000 paise)

        def worker(worker_idx):
            thread_conn = get_db_connection(db_path)
            thread_conn.current_user_id = 1
            for j in range(credits_per_thread):
                key = f"thread-{worker_idx}-tx-{j}"
                ledger.record_credit(thread_conn, customer_id=cid, amount_paise=amount_each, idempotency_key=key)
            thread_conn.close()

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            for f in futures:
                f.result()

        # Check final balance
        verify_conn = get_db_connection(db_path)
        verify_conn.current_user_id = 1
        bal = ledger.get_customer_balance(verify_conn, cid)
        expected_total = num_threads * credits_per_thread * amount_each  # 10 * 2 * 1000 = 20,000 paise (₹200)
        assert bal["balance_paise"] == expected_total
        assert bal["transaction_count"] == num_threads * credits_per_thread
        verify_conn.close()
        verify_conn = None
    finally:
        if init_conn:
            init_conn.close()
        if verify_conn:
            verify_conn.close()
        if os.path.exists(db_path):
            os.remove(db_path)


# ---------------------------------------------------------------------------
# 5. API Safety: Health Endpoint 503 on DB Failure
# ---------------------------------------------------------------------------

def test_health_check_returns_503_on_database_failure():
    """Verify health endpoint raises 503 Service Unavailable when DB fails."""
    broken_conn = get_db_connection(":memory:")
    broken_conn.close()  # Closed connection will fail SELECT 1

    def override_broken_db():
        yield broken_conn

    app.dependency_overrides[get_db] = override_broken_db
    client = TestClient(app)
    res = client.get("/health")
    assert res.status_code == 503
    assert "Database connection failure" in res.json()["detail"]
    app.dependency_overrides.clear()
