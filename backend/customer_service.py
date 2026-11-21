"""
Customer service module for Vaada Khata.
Handles customer creation with multiple aliases, alias-aware search,
and duplicate customer resolution with spoken clarification prompts.
Supports multi-merchant tenant isolation with explicit user_id scoping.
"""

import sqlite3
from typing import Dict, List, Optional
from backend.models import Customer
from backend.schemas import CustomerCreate, CustomerLookupResponse, CustomerLookupStatus, CustomerResponse


def get_customer_aliases(db: sqlite3.Connection, customer_id: int, user_id: Optional[int] = None) -> List[str]:
    """Retrieve all aliases associated with a customer ID."""
    cursor = db.cursor()
    if user_id is None:
        user_id = getattr(db, "current_user_id", None)
    if user_id is None:
        raise ValueError("Merchant user_id is required. Unauthenticated operation is rejected.")
    cursor.execute(
        "SELECT alias FROM customer_aliases WHERE user_id = ? AND customer_id = ? ORDER BY alias_id ASC",
        (user_id, customer_id),
    )
    rows = cursor.fetchall()
    return [row["alias"] for row in rows]


def get_bulk_customer_aliases(db: sqlite3.Connection, customer_ids: List[int], user_id: Optional[int] = None) -> Dict[int, List[str]]:
    """Retrieve aliases for multiple customer IDs efficiently."""
    if not customer_ids:
        return {}
    cursor = db.cursor()
    if user_id is None:
        user_id = getattr(db, "current_user_id", None)
    if user_id is None:
        raise ValueError("Merchant user_id is required. Unauthenticated operation is rejected.")
    placeholders = ",".join("?" for _ in customer_ids)
    cursor.execute(
        f"SELECT customer_id, alias FROM customer_aliases WHERE user_id = ? AND customer_id IN ({placeholders}) ORDER BY alias_id ASC",
        [user_id] + customer_ids,
    )
    rows = cursor.fetchall()
    result: Dict[int, List[str]] = {cid: [] for cid in customer_ids}
    for r in rows:
        result[r["customer_id"]].append(r["alias"])
    return result


def add_customer_alias(db: sqlite3.Connection, customer_id: int, alias: str, user_id: Optional[int] = None) -> bool:
    """Add a new alias to an existing customer. Returns True if added, False if duplicate."""
    clean_alias = alias.strip()
    if not clean_alias:
        return False
    if user_id is None:
        user_id = getattr(db, "current_user_id", None)
    if user_id is None:
        raise ValueError("Merchant user_id is required. Unauthenticated operation is rejected.")

    cursor = db.cursor()
    try:
        cursor.execute(
            "INSERT INTO customer_aliases (user_id, customer_id, alias) VALUES (?, ?, ?)",
            (user_id, customer_id, clean_alias),
        )
        db.commit()
        return True
    except sqlite3.IntegrityError:
        # Alias already exists for this customer (UNIQUE constraint) or composite FK failed
        return False


def create_customer(
    db: sqlite3.Connection,
    customer_in: CustomerCreate,
    user_id: Optional[int] = None,
) -> CustomerResponse:
    """
    Create a customer and store any provided aliases in the customer_aliases table.
    Validates that canonical name is non-empty. Requires explicit merchant user_id.
    Unauthenticated customer creation without user_id is rejected.
    """
    clean_name = customer_in.name.strip()
    if not clean_name:
        raise ValueError("Customer name cannot be empty.")

    if user_id is None:
        user_id = getattr(db, "current_user_id", None)
    if user_id is None:
        raise ValueError("Merchant user_id is required. Unauthenticated customer creation is rejected.")

    clean_phone = customer_in.phone.strip() if customer_in.phone else None

    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO customers (user_id, name, phone) VALUES (?, ?, ?)",
        (user_id, clean_name, clean_phone),
    )
    customer_id = cursor.lastrowid

    # Insert all aliases into customer_aliases
    aliases = customer_in.get_all_aliases()
    for alias_text in aliases:
        cursor.execute(
            "INSERT INTO customer_aliases (user_id, customer_id, alias) VALUES (?, ?, ?)",
            (user_id, customer_id, alias_text),
        )

    db.commit()

    cursor.execute("SELECT * FROM customers WHERE user_id = ? AND customer_id = ?", (user_id, customer_id))
    row = cursor.fetchone()
    stored_aliases = get_customer_aliases(db, customer_id, user_id=user_id)
    customer = Customer.from_row(row, aliases=stored_aliases)
    return CustomerResponse.model_validate(customer.to_dict())


def get_customer_by_id(
    db: sqlite3.Connection,
    customer_id: int,
    user_id: Optional[int] = None,
) -> Optional[CustomerResponse]:
    """Retrieve customer by their primary key, including all aliases."""
    if user_id is None:
        user_id = getattr(db, "current_user_id", None)
    if user_id is None:
        raise ValueError("Merchant user_id is required. Unauthenticated operation is rejected.")
    cursor = db.cursor()
    cursor.execute("SELECT * FROM customers WHERE customer_id = ? AND user_id = ?", (customer_id, user_id))
    row = cursor.fetchone()
    if not row:
        return None
    aliases = get_customer_aliases(db, customer_id, user_id=row["user_id"])
    customer = Customer.from_row(row, aliases=aliases)
    return CustomerResponse.model_validate(customer.to_dict())


def list_customers(
    db: sqlite3.Connection,
    limit: int = 100,
    offset: int = 0,
    user_id: Optional[int] = None,
) -> List[CustomerResponse]:
    """Retrieve paginated list of customers with all their aliases."""
    if user_id is None:
        user_id = getattr(db, "current_user_id", None)
    if user_id is None:
        raise ValueError("Merchant user_id is required. Unauthenticated operation is rejected.")
    cursor = db.cursor()
    cursor.execute(
        "SELECT * FROM customers WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    )
    rows = cursor.fetchall()
    customer_ids = [r["customer_id"] for r in rows]
    aliases_map = get_bulk_customer_aliases(db, customer_ids, user_id=user_id)

    result = []
    for r in rows:
        cid = r["customer_id"]
        c_aliases = aliases_map.get(cid, [])
        cust = Customer.from_row(r, aliases=c_aliases)
        result.append(CustomerResponse.model_validate(cust.to_dict()))
    return result


def _build_clarification_prompt(candidates: List[CustomerResponse], query_name: str) -> str:
    """
    Generate natural spoken Hindi/Hinglish clarification prompt for duplicate names,
    e.g. 'Auto wale Ramesh ya school wale Ramesh?'
    """
    options = []
    for c in candidates:
        if c.aliases:
            primary_alias = c.aliases[0]
            if c.name.lower() in primary_alias.lower():
                options.append(primary_alias)
            else:
                options.append(f"{primary_alias} {c.name}")
        elif c.phone:
            last4 = c.phone[-4:] if len(c.phone) >= 4 else c.phone
            options.append(f"{c.name} (phone ending in {last4})")
        else:
            options.append(f"{c.name} (ID {c.customer_id})")

    if len(options) == 2:
        return f"{options[0]} ya {options[1]}?"
    return f"Multiple customers found: {', '.join(options[:-1])} ya {options[-1]}?"


def lookup_customer(
    db: sqlite3.Connection,
    query: str,
    user_id: Optional[int] = None,
) -> CustomerLookupResponse:
    """
    Lookup customer by name or multiple aliases with intelligent duplicate resolution.
    Scoped by merchant user_id when provided or bound to connection.
    """
    clean_query = query.strip()
    if not clean_query:
        return CustomerLookupResponse(
            status=CustomerLookupStatus.NOT_FOUND,
            query=query,
            customer=None,
            candidates=[],
            clarification_prompt=None,
        )

    if user_id is None:
        user_id = getattr(db, "current_user_id", None)
    if user_id is None:
        raise ValueError("Merchant user_id is required. Unauthenticated operation is rejected.")

    cursor = db.cursor()

    # Step 1: Check exact canonical name match
    cursor.execute(
        "SELECT * FROM customers WHERE user_id = ? AND LOWER(name) = LOWER(?)",
        (user_id, clean_query),
    )
    exact_name_rows = cursor.fetchall()
    if exact_name_rows:
        customer_ids = [r["customer_id"] for r in exact_name_rows]
        aliases_map = get_bulk_customer_aliases(db, customer_ids, user_id=user_id)
        exact_candidates = [
            CustomerResponse.model_validate(
                Customer.from_row(r, aliases=aliases_map.get(r["customer_id"], [])).to_dict()
            )
            for r in exact_name_rows
        ]

        if len(exact_candidates) == 1:
            return CustomerLookupResponse(
                status=CustomerLookupStatus.EXACT_MATCH,
                query=clean_query,
                customer=exact_candidates[0],
                candidates=exact_candidates,
                clarification_prompt=None,
            )
        else:
            prompt = _build_clarification_prompt(exact_candidates, clean_query)
            return CustomerLookupResponse(
                status=CustomerLookupStatus.AMBIGUOUS_MATCH,
                query=clean_query,
                customer=None,
                candidates=exact_candidates,
                clarification_prompt=prompt,
            )

    # Step 2: Check exact alias match in customer_aliases table
    cursor.execute(
        """
        SELECT DISTINCT c.*
        FROM customers c
        JOIN customer_aliases ca ON ca.user_id = c.user_id AND ca.customer_id = c.customer_id
        WHERE c.user_id = ? AND LOWER(ca.alias) = LOWER(?)
        """,
        (user_id, clean_query),
    )
    exact_alias_rows = cursor.fetchall()
    if exact_alias_rows:
        customer_ids = [r["customer_id"] for r in exact_alias_rows]
        aliases_map = get_bulk_customer_aliases(db, customer_ids, user_id=user_id)
        exact_alias_candidates = [
            CustomerResponse.model_validate(
                Customer.from_row(r, aliases=aliases_map.get(r["customer_id"], [])).to_dict()
            )
            for r in exact_alias_rows
        ]

        if len(exact_alias_candidates) == 1:
            return CustomerLookupResponse(
                status=CustomerLookupStatus.EXACT_MATCH,
                query=clean_query,
                customer=exact_alias_candidates[0],
                candidates=exact_alias_candidates,
                clarification_prompt=None,
            )
        else:
            prompt = _build_clarification_prompt(exact_alias_candidates, clean_query)
            return CustomerLookupResponse(
                status=CustomerLookupStatus.AMBIGUOUS_MATCH,
                query=clean_query,
                customer=None,
                candidates=exact_alias_candidates,
                clarification_prompt=prompt,
            )

    # Step 3: Check composite matches (e.g. query is 'Auto wale Ramesh' or 'Ramesh Auto wale')
    cursor.execute("SELECT * FROM customers WHERE user_id = ?", (user_id,))
    all_rows = cursor.fetchall()
    all_cids = [r["customer_id"] for r in all_rows]
    all_aliases_map = get_bulk_customer_aliases(db, all_cids, user_id=user_id)
    all_customers = [
        CustomerResponse.model_validate(
            Customer.from_row(r, aliases=all_aliases_map.get(r["customer_id"], [])).to_dict()
        )
        for r in all_rows
    ]

    query_lower = clean_query.lower()
    composite_matches = []
    for c in all_customers:
        name_lower = c.name.lower()
        for a in c.aliases:
            a_lower = a.lower()
            if ((name_lower in query_lower and a_lower in query_lower) or
                (query_lower in f"{a_lower} {name_lower}") or
                (query_lower in f"{name_lower} {a_lower}")):
                if c not in composite_matches:
                    composite_matches.append(c)

    if len(composite_matches) == 1:
        return CustomerLookupResponse(
            status=CustomerLookupStatus.EXACT_MATCH,
            query=clean_query,
            customer=composite_matches[0],
            candidates=composite_matches,
            clarification_prompt=None,
        )
    elif len(composite_matches) > 1:
        prompt = _build_clarification_prompt(composite_matches, clean_query)
        return CustomerLookupResponse(
            status=CustomerLookupStatus.AMBIGUOUS_MATCH,
            query=clean_query,
            customer=None,
            candidates=composite_matches,
            clarification_prompt=prompt,
        )

    # Step 4: Substring / prefix search on name or any alias
    pattern = f"%{clean_query}%"
    cursor.execute(
        """
        SELECT DISTINCT c.*
        FROM customers c
        LEFT JOIN customer_aliases ca ON ca.user_id = c.user_id AND ca.customer_id = c.customer_id
        WHERE c.user_id = ? AND (c.name LIKE ? OR ca.alias LIKE ?)
        """,
        (user_id, pattern, pattern),
    )
    partial_rows = cursor.fetchall()
    if partial_rows:
        customer_ids = [r["customer_id"] for r in partial_rows]
        aliases_map = get_bulk_customer_aliases(db, customer_ids, user_id=user_id)
        partial_candidates = [
            CustomerResponse.model_validate(
                Customer.from_row(r, aliases=aliases_map.get(r["customer_id"], [])).to_dict()
            )
            for r in partial_rows
        ]

        if len(partial_candidates) == 1:
            return CustomerLookupResponse(
                status=CustomerLookupStatus.EXACT_MATCH,
                query=clean_query,
                customer=partial_candidates[0],
                candidates=partial_candidates,
                clarification_prompt=None,
            )
        else:
            prompt = _build_clarification_prompt(partial_candidates, clean_query)
            return CustomerLookupResponse(
                status=CustomerLookupStatus.AMBIGUOUS_MATCH,
                query=clean_query,
                customer=None,
                candidates=partial_candidates,
                clarification_prompt=prompt,
            )

    # Step 5: Customer not found
    return CustomerLookupResponse(
        status=CustomerLookupStatus.NOT_FOUND,
        query=clean_query,
        customer=None,
        candidates=[],
        clarification_prompt=None,
    )
