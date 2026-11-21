"""
Core Ledger Engine for Vaada Khata.
Implements:
1. Credit recording (Udhaar).
2. Payment recording.
3. Authoritative outstanding balance calculation (Source of Truth).
4. Transaction history.
5. Atomic writes using BEGIN IMMEDIATE and payload-bound idempotency.
6. Safe additive compensating reversals.
7. Backend-controlled pending proposals and explicit confirmation workflow.
"""

from decimal import Decimal
import sqlite3
from typing import Any, Dict, List, Optional
import uuid

from backend.database import atomic
from backend.models import PendingProposal, Transaction
from backend.money import parse_rupees_to_paise


class LedgerError(Exception):
    """Base exception for all ledger operations."""
    pass


class CustomerNotFoundError(LedgerError):
    """Raised when customer ID does not exist."""
    pass


class TransactionNotFoundError(LedgerError):
    """Raised when a transaction ID is not found."""
    pass


class ProposalNotFoundError(LedgerError):
    """Raised when a pending proposal ID is not found."""
    pass


class IdempotencyConflictError(LedgerError):
    """Raised when an idempotency key is reused with a different payload."""
    pass


class DuplicateReversalError(LedgerError):
    """Raised when attempting to reverse an already reversed transaction."""
    pass


class InvalidTransactionError(LedgerError):
    """Raised when transaction data violates business rules."""
    pass


def _get_customer(conn: sqlite3.Connection, customer_id: int) -> sqlite3.Row:
    """Fetch customer record or raise CustomerNotFoundError."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customers WHERE customer_id = ?", (customer_id,))
    row = cursor.fetchone()
    if not row:
        raise CustomerNotFoundError(f"Customer with ID {customer_id} does not exist.")
    return row


def record_credit(
    conn: sqlite3.Connection,
    customer_id: int,
    amount_paise: int,
    item: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Transaction:
    """
    Record customer credit (Udhaar).
    Increases customer's outstanding balance.
    Atomic write using BEGIN IMMEDIATE.
    """
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int) or amount_paise <= 0:
        raise InvalidTransactionError(f"Credit amount must be greater than zero, got {amount_paise}.")

    clean_item = item.strip() if item else None
    clean_key = idempotency_key.strip() if idempotency_key else None

    with atomic(conn):
        _get_customer(conn, customer_id)

        # Idempotency check: if key already exists, verify payload
        if clean_key:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM transactions WHERE idempotency_key = ?",
                (clean_key,),
            )
            existing = cursor.fetchone()
            if existing:
                if (
                    existing["customer_id"] == customer_id
                    and existing["type"] == "CREDIT"
                    and existing["amount_paise"] == amount_paise
                    and (existing["item"] or None) == clean_item
                ):
                    return Transaction.from_row(existing)
                raise IdempotencyConflictError(
                    f"Idempotency key '{clean_key}' was previously used with a different transaction payload."
                )

        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO transactions (customer_id, type, amount_paise, item, status, idempotency_key)
            VALUES (?, 'CREDIT', ?, ?, 'COMMITTED', ?)
            """,
            (customer_id, amount_paise, clean_item, clean_key),
        )
        tx_id = cursor.lastrowid
        cursor.execute("SELECT * FROM transactions WHERE transaction_id = ?", (tx_id,))
        row = cursor.fetchone()
        return Transaction.from_row(row)


def record_payment(
    conn: sqlite3.Connection,
    customer_id: int,
    amount_paise: int,
    payment_method: Optional[str] = None,
    item: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Transaction:
    """
    Record customer payment.
    Decreases customer's outstanding balance.
    Atomic write using BEGIN IMMEDIATE.
    """
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int) or amount_paise <= 0:
        raise InvalidTransactionError(f"Payment amount must be greater than zero, got {amount_paise}.")

    clean_method = payment_method.strip().upper() if payment_method else None
    if clean_method and clean_method not in ("CASH", "UPI", "OTHER"):
        raise InvalidTransactionError(
            f"Invalid payment method: {payment_method}. Must be CASH, UPI, or OTHER."
        )

    clean_item = item.strip() if item else None
    clean_key = idempotency_key.strip() if idempotency_key else None

    with atomic(conn):
        _get_customer(conn, customer_id)

        # Idempotency check: if key already exists, verify payload
        if clean_key:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM transactions WHERE idempotency_key = ?",
                (clean_key,),
            )
            existing = cursor.fetchone()
            if existing:
                if (
                    existing["customer_id"] == customer_id
                    and existing["type"] == "PAYMENT"
                    and existing["amount_paise"] == amount_paise
                    and (existing["payment_method"] or None) == clean_method
                    and (existing["item"] or None) == clean_item
                ):
                    return Transaction.from_row(existing)
                raise IdempotencyConflictError(
                    f"Idempotency key '{clean_key}' was previously used with a different transaction payload."
                )

        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO transactions (customer_id, type, amount_paise, payment_method, item, status, idempotency_key)
            VALUES (?, 'PAYMENT', ?, ?, ?, 'COMMITTED', ?)
            """,
            (customer_id, amount_paise, clean_method, clean_item, clean_key),
        )
        tx_id = cursor.lastrowid
        cursor.execute("SELECT * FROM transactions WHERE transaction_id = ?", (tx_id,))
        row = cursor.fetchone()
        return Transaction.from_row(row)


def get_customer_balance(conn: sqlite3.Connection, customer_id: int) -> Dict[str, Any]:
    """
    Calculate customer's authoritative outstanding balance directly from committed transactions in SQLite.

    Formula:
      Outstanding Balance (Paise) = Total Credits - Total Payments.

    The database is the sole source of truth. Never trusts AI or cached balances.
    """
    customer = _get_customer(conn, customer_id)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type = 'CREDIT' THEN amount_paise ELSE 0 END), 0) AS total_credit_paise,
            COALESCE(SUM(CASE WHEN type = 'PAYMENT' THEN amount_paise ELSE 0 END), 0) AS total_payment_paise,
            COUNT(*) AS transaction_count
        FROM transactions
        WHERE customer_id = ?
        """,
        (customer_id,),
    )
    row = cursor.fetchone()
    total_credit = row["total_credit_paise"]
    total_payment = row["total_payment_paise"]
    balance_paise = total_credit - total_payment

    return {
        "customer_id": customer_id,
        "customer_name": customer["name"],
        "balance_paise": balance_paise,
        "balance_rupees": balance_paise / 100.0,
        "total_credit_paise": total_credit,
        "total_credit_rupees": total_credit / 100.0,
        "total_payment_paise": total_payment,
        "total_payment_rupees": total_payment / 100.0,
        "transaction_count": row["transaction_count"],
    }


def get_customer_transactions(
    conn: sqlite3.Connection, customer_id: int, limit: int = 100, offset: int = 0
) -> List[Transaction]:
    """
    Retrieve chronological transaction history for an individual customer.
    All historical entries, reversals, and compensating rows are preserved.
    """
    _get_customer(conn, customer_id)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM transactions
        WHERE customer_id = ?
        ORDER BY transaction_date DESC, transaction_id DESC
        LIMIT ? OFFSET ?
        """,
        (customer_id, limit, offset),
    )
    rows = cursor.fetchall()
    return [Transaction.from_row(r) for r in rows]


def reverse_transaction(
    conn: sqlite3.Connection,
    transaction_id: int,
    reason: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Perform a safe additive compensating reversal.
    Rules:
    1. Preserves original transaction: updates status to REVERSED.
    2. Creates an opposite compensating transaction:
       - Original CREDIT -> Compensating PAYMENT.
       - Original PAYMENT -> Compensating CREDIT.
    3. Links the compensating entry to the original via reversed_transaction_id.
    4. Uniqueness on reversed_transaction_id guarantees duplicate reversals are rejected.
    5. Atomic execution under BEGIN IMMEDIATE.
    """
    clean_key = idempotency_key.strip() if idempotency_key else None
    clean_reason = reason.strip() if reason else None

    with atomic(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,))
        orig_row = cursor.fetchone()
        if not orig_row:
            raise TransactionNotFoundError(f"Transaction with ID {transaction_id} not found.")

        orig = Transaction.from_row(orig_row)

        if orig.reversed_transaction_id is not None:
            raise InvalidTransactionError(
                f"Transaction {transaction_id} is already a compensating reversal and cannot be reversed."
            )

        # Check if already reversed (idempotent replay check)
        cursor.execute(
            "SELECT * FROM transactions WHERE reversed_transaction_id = ?",
            (transaction_id,),
        )
        existing_reversal = cursor.fetchone()
        if existing_reversal:
            comp_tx = Transaction.from_row(existing_reversal)
            bal = get_customer_balance(conn, orig.customer_id)
            return {
                "reversed_transaction": orig.to_dict(),
                "compensating_transaction": comp_tx.to_dict(),
                "balance_paise": bal["balance_paise"],
                "balance_rupees": bal["balance_rupees"],
                "message": f"Transaction {transaction_id} was already reversed (idempotent replay).",
            }

        comp_type = "PAYMENT" if orig.type == "CREDIT" else "CREDIT"
        comp_item = (
            f"Reversal of transaction #{orig.transaction_id}"
            + (f": {clean_reason}" if clean_reason else "")
        )

        cursor.execute(
            "UPDATE transactions SET status = 'REVERSED' WHERE transaction_id = ?",
            (transaction_id,),
        )

        try:
            cursor.execute(
                """
                INSERT INTO transactions (
                    customer_id, type, amount_paise, item, status,
                    reversed_transaction_id, idempotency_key
                )
                VALUES (?, ?, ?, ?, 'COMMITTED', ?, ?)
                """,
                (orig.customer_id, comp_type, orig.amount_paise, comp_item, orig.transaction_id, clean_key),
            )
        except sqlite3.IntegrityError as ie:
            if "UNIQUE constraint failed" in str(ie) and "reversed_transaction_id" in str(ie):
                raise DuplicateReversalError(f"Transaction {transaction_id} has already been reversed.") from ie
            raise

        comp_id = cursor.lastrowid
        cursor.execute("SELECT * FROM transactions WHERE transaction_id = ?", (comp_id,))
        comp_row = cursor.fetchone()
        comp_tx = Transaction.from_row(comp_row)

        cursor.execute("SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,))
        updated_orig_row = cursor.fetchone()
        updated_orig = Transaction.from_row(updated_orig_row)

        bal = get_customer_balance(conn, orig.customer_id)
        return {
            "reversed_transaction": updated_orig.to_dict(),
            "compensating_transaction": comp_tx.to_dict(),
            "balance_paise": bal["balance_paise"],
            "balance_rupees": bal["balance_rupees"],
            "message": f"Transaction {transaction_id} reversed successfully.",
        }


# ---------------------------------------------------------------------------
# Backend-Controlled Proposal & Confirmation Engine
# ---------------------------------------------------------------------------

def create_pending_proposal(
    conn: sqlite3.Connection,
    customer_id: int,
    intent: str,
    amount_paise: int,
    item: Optional[str] = None,
    payment_method: Optional[str] = None,
) -> PendingProposal:
    """
    Store an AI transaction proposal awaiting explicit merchant confirmation.
    Does NOT write to the ledger.
    """
    _get_customer(conn, customer_id)
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int) or amount_paise <= 0:
        raise InvalidTransactionError(f"Proposal amount must be a positive integer in paise, got {amount_paise}.")

    clean_intent = intent.strip().lower()
    if clean_intent not in ("add_credit", "record_payment", "credit", "payment"):
        raise InvalidTransactionError(f"Unsupported intent for proposal: {intent}")

    clean_method = payment_method.strip().upper() if payment_method else None
    clean_item = item.strip() if item else None
    proposal_id = f"prop_{uuid.uuid4().hex[:12]}"

    with atomic(conn):
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO pending_proposals (
                proposal_id, customer_id, intent, amount_paise, item, payment_method, status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (proposal_id, customer_id, clean_intent, amount_paise, clean_item, clean_method),
        )
        cursor.execute("SELECT * FROM pending_proposals WHERE proposal_id = ?", (proposal_id,))
        row = cursor.fetchone()
        return PendingProposal.from_row(row)


def get_pending_proposal(conn: sqlite3.Connection, proposal_id: str) -> Optional[PendingProposal]:
    """Retrieve pending proposal by proposal_id."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM pending_proposals WHERE proposal_id = ?", (proposal_id,))
    row = cursor.fetchone()
    if not row:
        return None
    return PendingProposal.from_row(row)


def confirm_pending_proposal(conn: sqlite3.Connection, proposal_id: str) -> Dict[str, Any]:
    """
    Explicit confirmation: commits a pending proposal into SQLite.
    Idempotent: repeating confirmation for the same proposal_id returns the existing
    committed transaction without duplicating entries.
    """
    with atomic(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pending_proposals WHERE proposal_id = ?", (proposal_id,))
        row = cursor.fetchone()
        if not row:
            raise ProposalNotFoundError(f"Proposal '{proposal_id}' does not exist.")

        prop = PendingProposal.from_row(row)

        # Idempotent replay: if already confirmed, return existing transaction
        if prop.status == "CONFIRMED" and prop.committed_transaction_id is not None:
            cursor.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?",
                (prop.committed_transaction_id,),
            )
            tx_row = cursor.fetchone()
            tx = Transaction.from_row(tx_row)
            bal = get_customer_balance(conn, prop.customer_id)
            return {
                "proposal_id": proposal_id,
                "status": "CONFIRMED",
                "committed": True,
                "transaction": tx.to_dict(),
                "customer_balance": bal,
                "message": "Proposal was already confirmed (idempotent replay).",
            }

        if prop.status == "CANCELLED":
            raise InvalidTransactionError(f"Proposal '{proposal_id}' was cancelled and cannot be confirmed.")

        # Execute ledger operation using unique proposal idempotency key
        prop_key = f"proposal-{proposal_id}"
        if prop.intent in ("add_credit", "credit"):
            tx = record_credit(
                conn,
                customer_id=prop.customer_id,
                amount_paise=prop.amount_paise,
                item=prop.item,
                idempotency_key=prop_key,
            )
        else:
            tx = record_payment(
                conn,
                customer_id=prop.customer_id,
                amount_paise=prop.amount_paise,
                payment_method=prop.payment_method,
                item=prop.item,
                idempotency_key=prop_key,
            )

        cursor.execute(
            """
            UPDATE pending_proposals
            SET status = 'CONFIRMED', committed_transaction_id = ?
            WHERE proposal_id = ?
            """,
            (tx.transaction_id, proposal_id),
        )

        bal = get_customer_balance(conn, prop.customer_id)
        return {
            "proposal_id": proposal_id,
            "status": "CONFIRMED",
            "committed": True,
            "transaction": tx.to_dict(),
            "customer_balance": bal,
            "message": "Proposal confirmed and committed successfully.",
        }


def cancel_pending_proposal(conn: sqlite3.Connection, proposal_id: str) -> Dict[str, Any]:
    """
    Cancel a pending proposal (e.g. merchant said 'Nahi, rehne do').
    """
    with atomic(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pending_proposals WHERE proposal_id = ?", (proposal_id,))
        row = cursor.fetchone()
        if not row:
            raise ProposalNotFoundError(f"Proposal '{proposal_id}' does not exist.")

        prop = PendingProposal.from_row(row)
        if prop.status == "CONFIRMED":
            raise InvalidTransactionError(f"Proposal '{proposal_id}' has already been confirmed and committed.")

        cursor.execute(
            "UPDATE pending_proposals SET status = 'CANCELLED' WHERE proposal_id = ?",
            (proposal_id,),
        )
        return {
            "proposal_id": proposal_id,
            "status": "CANCELLED",
            "message": "Proposal cancelled successfully.",
        }
