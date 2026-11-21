"""
Tests for customer creation, multiple customer aliases, lookup by names/aliases,
and duplicate customer handling.
"""

import pytest
from backend.database import SCHEMA_SQL, get_db_connection
from backend.schemas import CustomerCreate, CustomerLookupStatus
import backend.customer_service as customer_service


@pytest.fixture
def db():
    """Provides an isolated in-memory database with schema."""
    conn = get_db_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.current_user_id = 1
    yield conn
    conn.close()


def test_create_customer_with_multiple_aliases(db):
    """
    MULTIPLE CUSTOMER ALIASES:
    Verify customer creation with multiple aliases stored in customer_aliases table.
    """
    new_cust = CustomerCreate(
        name="Ramesh",
        alias="Auto wale",
        aliases=["Auto driver", "Driver Ramesh"],
        phone="9876543210"
    )
    created = customer_service.create_customer(db, new_cust)

    assert created.customer_id is not None
    assert created.name == "Ramesh"
    assert created.phone == "9876543210"
    assert created.alias == "Auto wale"
    # All unique aliases should be present
    assert "Auto wale" in created.aliases
    assert "Auto driver" in created.aliases
    assert "Driver Ramesh" in created.aliases
    assert len(created.aliases) == 3

    # Check database rows in customer_aliases table
    cursor = db.cursor()
    cursor.execute("SELECT alias FROM customer_aliases WHERE customer_id = ?", (created.customer_id,))
    stored = {r[0] for r in cursor.fetchall()}
    assert stored == {"Auto wale", "Auto driver", "Driver Ramesh"}


def test_add_customer_alias_to_existing_customer(db):
    """Verify adding an alias to an already existing customer."""
    created = customer_service.create_customer(db, CustomerCreate(name="Suresh", alias="Tailor"))
    assert len(created.aliases) == 1

    added = customer_service.add_customer_alias(db, created.customer_id, "Shop owner")
    assert added is True

    # Re-adding the exact same alias should be rejected gracefully by UNIQUE constraint
    duplicate_added = customer_service.add_customer_alias(db, created.customer_id, "Shop owner")
    assert duplicate_added is False

    updated = customer_service.get_customer_by_id(db, created.customer_id)
    assert "Tailor" in updated.aliases
    assert "Shop owner" in updated.aliases
    assert len(updated.aliases) == 2


def test_alias_based_customer_lookup_any_alias(db):
    """
    ALIAS-BASED LOOKUP:
    Verify that querying ANY of a customer's registered aliases resolves to that customer.
    """
    customer_service.create_customer(
        db,
        CustomerCreate(
            name="Ramesh",
            aliases=["Auto wale", "Seven seater driver", "Auto Ramesh"]
        )
    )

    # Querying primary alias
    r1 = customer_service.lookup_customer(db, "Auto wale")
    assert r1.status == CustomerLookupStatus.EXACT_MATCH
    assert r1.customer.name == "Ramesh"

    # Querying secondary alias
    r2 = customer_service.lookup_customer(db, "Seven seater driver")
    assert r2.status == CustomerLookupStatus.EXACT_MATCH
    assert r2.customer.name == "Ramesh"

    # Querying tertiary alias
    r3 = customer_service.lookup_customer(db, "Auto Ramesh")
    assert r3.status == CustomerLookupStatus.EXACT_MATCH
    assert r3.customer.name == "Ramesh"


def test_duplicate_customers_triggers_ambiguous_match(db):
    """
    DUPLICATE CUSTOMER RESOLUTION:
    When two customers share the name 'Ramesh', lookup must return AMBIGUOUS_MATCH
    with both candidates and a spoken clarification prompt.
    """
    c1 = customer_service.create_customer(db, CustomerCreate(name="Ramesh", alias="Auto wale"))
    c2 = customer_service.create_customer(db, CustomerCreate(name="Ramesh", alias="School wale"))

    result = customer_service.lookup_customer(db, "Ramesh")

    assert result.status == CustomerLookupStatus.AMBIGUOUS_MATCH
    assert result.customer is None
    assert len(result.candidates) == 2
    candidate_ids = {c.customer_id for c in result.candidates}
    assert c1.customer_id in candidate_ids
    assert c2.customer_id in candidate_ids

    # Prompt should ask shopkeeper to clarify: 'Auto wale Ramesh ya school wale Ramesh?'
    assert result.clarification_prompt is not None
    assert "Auto wale" in result.clarification_prompt
    assert "School wale" in result.clarification_prompt
    assert "ya" in result.clarification_prompt


def test_disambiguation_between_duplicates_using_specific_alias(db):
    """
    Verify that using one of the specific aliases immediately resolves
    ambiguity between two duplicate customers.
    """
    c1 = customer_service.create_customer(
        db, CustomerCreate(name="Ramesh", aliases=["Auto wale", "DL 4567"])
    )
    c2 = customer_service.create_customer(
        db, CustomerCreate(name="Ramesh", aliases=["School wale", "Bus 12"])
    )

    # Disambiguates to Customer 1 via secondary alias
    res1 = customer_service.lookup_customer(db, "DL 4567")
    assert res1.status == CustomerLookupStatus.EXACT_MATCH
    assert res1.customer.customer_id == c1.customer_id

    # Disambiguates to Customer 2 via secondary alias
    res2 = customer_service.lookup_customer(db, "Bus 12")
    assert res2.status == CustomerLookupStatus.EXACT_MATCH
    assert res2.customer.customer_id == c2.customer_id


def test_composite_name_and_alias_lookup(db):
    """Test composite query like 'Auto wale Ramesh'."""
    c1 = customer_service.create_customer(db, CustomerCreate(name="Ramesh", alias="Auto wale"))
    customer_service.create_customer(db, CustomerCreate(name="Ramesh", alias="School wale"))

    res = customer_service.lookup_customer(db, "Auto wale Ramesh")
    assert res.status == CustomerLookupStatus.EXACT_MATCH
    assert res.customer.customer_id == c1.customer_id


def test_create_customer_empty_name_fails(db):
    """Test that customer creation fails on empty or whitespace name."""
    with pytest.raises(ValueError, match="Customer name cannot be empty"):
        customer_service.create_customer(db, CustomerCreate(name="   "))


def test_lookup_not_found(db):
    """Test lookup returns NOT_FOUND when customer does not exist."""
    result = customer_service.lookup_customer(db, "NonExistentPerson")
    assert result.status == CustomerLookupStatus.NOT_FOUND
    assert result.customer is None
    assert len(result.candidates) == 0
