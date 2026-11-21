"""
API integration tests for FastAPI endpoints, money precision, and multiple aliases.
"""

import pytest
from fastapi.testclient import TestClient

from backend.database import SCHEMA_SQL, get_db, get_db_connection
from backend.main import app


@pytest.fixture
def test_client():
    """Provides a TestClient with an isolated in-memory database overriding get_db."""
    conn = get_db_connection(":memory:")
    conn.current_user_id = 1  # Explicitly scope test client to test merchant 1
    conn.executescript(SCHEMA_SQL)
    conn.commit()

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


def test_health_endpoint(test_client):
    """Test health check endpoint."""
    response = test_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["database"] == "connected"


def test_create_and_get_customer_with_multiple_aliases(test_client):
    """Test creating a customer with multiple aliases via API and retrieving them."""
    payload = {
        "name": "Lakshmi",
        "alias": "Tailor",
        "aliases": ["Master Tailor", "Dress Maker"],
        "phone": "9123456780"
    }
    response = test_client.post("/customers", json=payload)
    assert response.status_code == 201
    created = response.json()
    assert created["name"] == "Lakshmi"
    assert created["alias"] == "Tailor"
    assert "Tailor" in created["aliases"]
    assert "Master Tailor" in created["aliases"]
    assert "Dress Maker" in created["aliases"]
    customer_id = created["customer_id"]

    # Retrieve by ID
    get_res = test_client.get(f"/customers/{customer_id}")
    assert get_res.status_code == 200
    retrieved = get_res.json()
    assert retrieved["name"] == "Lakshmi"
    assert len(retrieved["aliases"]) == 3


def test_create_customer_invalid_data(test_client):
    """Test customer creation validation with empty name."""
    response = test_client.post("/customers", json={"name": "   "})
    assert response.status_code == 400


def test_lookup_endpoint_exact_and_duplicate(test_client):
    """
    Test customer lookup endpoint:
    - Single customer: EXACT_MATCH
    - Duplicate names: AMBIGUOUS_MATCH with clarification prompt
    """
    # Create single customer Suresh
    test_client.post("/customers", json={"name": "Suresh", "aliases": ["Provision Store"]})
    res1 = test_client.get("/customers/lookup?q=Suresh")
    assert res1.status_code == 200
    assert res1.json()["status"] == "EXACT_MATCH"
    assert res1.json()["customer"]["name"] == "Suresh"

    # Create two duplicate customers named Ramesh
    test_client.post("/customers", json={"name": "Ramesh", "alias": "Auto wale"})
    test_client.post("/customers", json={"name": "Ramesh", "alias": "School wale"})

    res2 = test_client.get("/customers/lookup?q=Ramesh")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "AMBIGUOUS_MATCH"
    assert len(data2["candidates"]) == 2
    assert "Auto wale" in data2["clarification_prompt"]
    assert "School wale" in data2["clarification_prompt"]


def test_ai_to_backend_json_contract_exact_structure(test_client):
    """
    Verify the strict AI-to-Backend JSON Contract:
    {
      "intent": "add_credit",
      "customer_name": "Ramesh",
      "amount": 300,
      "item": null,
      "confirmation_required": true
    }
    """
    contract_payload = {
        "intent": "add_credit",
        "customer_name": "Ramesh",
        "amount": 300,
        "item": None,
        "confirmation_required": True
    }

    response = test_client.post("/ai/contract/validate", json=contract_payload)
    assert response.status_code == 200
    data = response.json()
    assert data["contract_valid"] is True
    assert data["amount_paise"] == 30000
    assert data["received_proposal"]["intent"] == "add_credit"
    assert data["received_proposal"]["customer_name"] == "Ramesh"
    assert data["received_proposal"]["amount"] == 300
    assert data["received_proposal"]["item"] is None
    assert data["received_proposal"]["confirmation_required"] is True


def test_ai_contract_exact_paise_conversion_19_99(test_client):
    """Verify that ₹19.99 in AI proposal converts cleanly to 1999 paise."""
    payload = {
        "intent": "add_credit",
        "customer_name": "Ramesh",
        "amount": 19.99,
        "item": "Soap",
        "confirmation_required": True
    }
    response = test_client.post("/ai/contract/validate", json=payload)
    assert response.status_code == 200
    assert response.json()["amount_paise"] == 1999


def test_ai_contract_exact_paise_conversion_430_50(test_client):
    """Verify that ₹430.50 in AI proposal converts cleanly to 43050 paise."""
    payload = {
        "intent": "add_credit",
        "customer_name": "Ramesh",
        "amount": 430.50,
        "item": "Rice",
        "confirmation_required": True
    }
    response = test_client.post("/ai/contract/validate", json=payload)
    assert response.status_code == 200
    assert response.json()["amount_paise"] == 43050


def test_ai_contract_rejects_invalid_precision_and_negative_amounts(test_client):
    """Verify rejection of negative amounts and amounts with more than 2 decimal places."""
    # Negative amount
    res_neg = test_client.post(
        "/ai/contract/validate",
        json={"intent": "add_credit", "customer_name": "Ramesh", "amount": -50, "item": None, "confirmation_required": True}
    )
    assert res_neg.status_code == 422

    # More than 2 decimal places (fractional paise)
    res_prec = test_client.post(
        "/ai/contract/validate",
        json={"intent": "add_credit", "customer_name": "Ramesh", "amount": 10.125, "item": None, "confirmation_required": True}
    )
    assert res_prec.status_code == 422


def test_ai_contract_preserves_numeric_representation_at_api_boundary(test_client):
    """
    Verify that incoming numeric amounts (integer or float) remain numeric in JSON
    and do not get transformed into unexpected types or formats.
    """
    # 1. Integer amount 300
    res_int = test_client.post(
        "/ai/contract/validate",
        json={"intent": "add_credit", "customer_name": "Ramesh", "amount": 300, "item": None, "confirmation_required": True}
    )
    assert res_int.status_code == 200
    val_int = res_int.json()["received_proposal"]["amount"]
    assert isinstance(val_int, (int, float))
    assert val_int == 300

    # 2. Float amount 430.50
    res_float = test_client.post(
        "/ai/contract/validate",
        json={"intent": "add_credit", "customer_name": "Ramesh", "amount": 430.50, "item": None, "confirmation_required": True}
    )
    assert res_float.status_code == 200
    val_float = res_float.json()["received_proposal"]["amount"]
    assert isinstance(val_float, (int, float))
    assert val_float == 430.5
