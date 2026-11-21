"""
API integration tests for Milestone 2:
Credit, Payment, Balance, Transactions, Reversals, and the AI Contract Execution Gate.
"""

import pytest
from fastapi.testclient import TestClient

from backend.database import SCHEMA_SQL, get_db, get_db_connection, run_migrations
from backend.main import app


@pytest.fixture
def test_client():
    """
    Provides an authenticated TestClient backed by an isolated in-memory database.
    Registers a test merchant and returns a client pre-loaded with a valid Bearer token.
    """
    conn = get_db_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)

    def override_get_db():
        try:
            yield conn
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    # Register a test merchant and capture their Bearer token
    reg = client.post("/auth/register", json={
        "phone": "9000000002",
        "name": "Test Merchant M2",
        "shop_name": "Test Shop M2",
        "pin": "246813",
    })
    assert reg.status_code == 201, f"Fixture registration failed: {reg.json()}"
    token = reg.json()["token"]
    client.headers.update({"Authorization": f"Bearer {token}"})

    yield client
    app.dependency_overrides.clear()
    conn.close()


def test_api_credit_and_balance_endpoints(test_client):
    """Test POST credit and GET balance via API."""
    # 1. Create customer
    c_res = test_client.post("/customers", json={"name": "Ramesh", "alias": "Auto wale"})
    assert c_res.status_code == 201
    cid = c_res.json()["customer_id"]

    # 2. Add credit
    cr_res = test_client.post(
        f"/customers/{cid}/credit",
        json={"amount": 340.50, "item": "Cooking oil"}
    )
    assert cr_res.status_code == 201
    cr_data = cr_res.json()
    assert cr_data["amount_paise"] == 34050
    assert cr_data["type"] == "CREDIT"
    assert cr_data["item"] == "Cooking oil"

    # 3. Check balance
    bal_res = test_client.get(f"/customers/{cid}/balance")
    assert bal_res.status_code == 200
    bal_data = bal_res.json()
    assert bal_data["balance_paise"] == 34050
    assert bal_data["balance_rupees"] == 340.50


def test_api_credit_invalid_amount_or_missing_customer(test_client):
    """Test 404 on missing customer and 400 on invalid amount."""
    # Missing customer
    res_404 = test_client.post("/customers/99999/credit", json={"amount": 100})
    assert res_404.status_code == 404

    # Invalid amount
    c_res = test_client.post("/customers", json={"name": "Suresh"})
    cid = c_res.json()["customer_id"]
    res_400 = test_client.post(f"/customers/{cid}/credit", json={"amount": -50})
    assert res_400.status_code == 400


def test_api_payment_endpoint(test_client):
    """Test recording payment via API."""
    c_res = test_client.post("/customers", json={"name": "Ramesh"})
    cid = c_res.json()["customer_id"]

    # Credit ₹500
    test_client.post(f"/customers/{cid}/credit", json={"amount": 500})

    # Payment ₹200 via UPI
    pay_res = test_client.post(
        f"/customers/{cid}/payment",
        json={"amount": 200, "payment_method": "UPI", "item": "Google Pay"}
    )
    assert pay_res.status_code == 201
    pay_data = pay_res.json()
    assert pay_data["amount_paise"] == 20000
    assert pay_data["payment_method"] == "UPI"
    assert pay_data["type"] == "PAYMENT"

    # Balance check: 500 - 200 = ₹300
    bal_res = test_client.get(f"/customers/{cid}/balance")
    assert bal_res.json()["balance_paise"] == 30000
    assert bal_res.json()["balance_rupees"] == 300.0


def test_api_payment_invalid_method(test_client):
    """Test invalid payment method rejected with 400."""
    c_res = test_client.post("/customers", json={"name": "Ramesh"})
    cid = c_res.json()["customer_id"]

    res = test_client.post(
        f"/customers/{cid}/payment",
        json={"amount": 100, "payment_method": "DOGECOIN"}
    )
    assert res.status_code == 400


def test_api_transactions_history(test_client):
    """Test customer transactions history endpoint."""
    c_res = test_client.post("/customers", json={"name": "Lakshmi"})
    cid = c_res.json()["customer_id"]

    test_client.post(f"/customers/{cid}/credit", json={"amount": 150})
    test_client.post(f"/customers/{cid}/credit", json={"amount": 250})

    history_res = test_client.get(f"/customers/{cid}/transactions")
    assert history_res.status_code == 200
    txs = history_res.json()
    assert len(txs) == 2


def test_api_reversal_endpoint(test_client):
    """Test transaction reversal via API."""
    c_res = test_client.post("/customers", json={"name": "Ramesh"})
    cid = c_res.json()["customer_id"]

    # 1. Add credit ₹500
    cr_res = test_client.post(f"/customers/{cid}/credit", json={"amount": 500})
    tx_id = cr_res.json()["transaction_id"]

    # 2. Reverse transaction
    rev_res = test_client.post(
        f"/transactions/{tx_id}/reverse",
        json={"reason": "Shopkeeper entered 500 instead of 300"}
    )
    assert rev_res.status_code == 200
    rev_data = rev_res.json()
    assert rev_data["reversed_transaction"]["status"] == "REVERSED"
    assert rev_data["compensating_transaction"]["type"] == "PAYMENT"
    assert rev_data["balance_paise"] == 0

    # 3. Add corrected transaction ₹300
    test_client.post(f"/customers/{cid}/credit", json={"amount": 300})

    # Balance check: should be exactly ₹300
    bal_res = test_client.get(f"/customers/{cid}/balance")
    assert bal_res.json()["balance_paise"] == 30000


def test_ai_contract_execution_gate_prevents_unconfirmed_commits(test_client):
    """
    AI CONTRACT SAFETY GATE:
    The backend must NOT commit a financial transaction while confirmation_required is True.
    """
    test_client.post("/customers", json={"name": "Ramesh"})

    unconfirmed_payload = {
        "intent": "add_credit",
        "customer_name": "Ramesh",
        "amount": 300,
        "item": "Rice 5kg",
        "confirmation_required": True  # Unconfirmed proposal
    }

    # Attempting to commit unconfirmed transaction must be REJECTED
    res = test_client.post("/ai/contract/commit", json=unconfirmed_payload)
    assert res.status_code == 400
    assert "confirmation_required is True" in res.json()["detail"]


def test_ai_contract_commit_succeeds_when_confirmed(test_client):
    """
    AI CONTRACT SAFETY GATE:
    When confirmation_required is False (merchant said 'Haan'), transaction is committed.
    """
    c_res = test_client.post("/customers", json={"name": "Suresh", "alias": "Provision"})
    cid = c_res.json()["customer_id"]

    confirmed_payload = {
        "intent": "add_credit",
        "customer_name": "Suresh",
        "amount": 430.50,
        "item": "Dal & Ghee",
        "confirmation_required": False  # Confirmed by shopkeeper!
    }

    commit_res = test_client.post("/ai/contract/commit", json=confirmed_payload)
    assert commit_res.status_code == 200
    commit_data = commit_res.json()
    assert commit_data["committed"] is True
    assert commit_data["transaction"]["amount_paise"] == 43050
    assert commit_data["transaction"]["type"] == "CREDIT"
    assert commit_data["customer_balance"]["balance_paise"] == 43050


def test_ai_contract_commit_rejects_ambiguous_customer_names(test_client):
    """
    AI CONTRACT SAFETY GATE:
    When two customers share the name 'Ramesh', commit is blocked and prompts for clarification.
    """
    test_client.post("/customers", json={"name": "Ramesh", "alias": "Auto wale"})
    test_client.post("/customers", json={"name": "Ramesh", "alias": "School wale"})

    ambiguous_payload = {
        "intent": "add_credit",
        "customer_name": "Ramesh",  # Ambiguous!
        "amount": 200,
        "item": None,
        "confirmation_required": False
    }

    commit_res = test_client.post("/ai/contract/commit", json=ambiguous_payload)
    assert commit_res.status_code == 409
    detail = commit_res.json()["detail"]
    assert "ambiguous customer name" in detail["message"]
    assert "Auto wale" in detail["clarification_prompt"]
    assert "School wale" in detail["clarification_prompt"]
