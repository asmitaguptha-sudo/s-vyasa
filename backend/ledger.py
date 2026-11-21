"""
Core ledger operations and accounting rules for Vaada Khata.
Strictly enforces:
- Integer paise only: no floats, rounding, or fractions.
- Double-entry / additive compensating reversals: original transactions are immutable.
- Backend-authoritative balance: derived dynamically as sum(CREDIT) - sum(PAYMENT).
- Multi-merchant account isolation with composite foreign keys and explicit user_id scoping.
- Atomic write transactions under SQLite BEGIN IMMEDIATE.
"""

from decimal import Decimal
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
import uuid

from backend.database import atomic
from backend.models import PendingProposal, Transaction
from backend.schemas import ProposalConfirmResponse, ProposalResponse


class LedgerError(Exception):
    """Base exception for all ledger-related domain errors."""
    pass


class CustomerNotFoundError(LedgerError):
    """Raised when customer ID is not found in database."""
    pass


class TransactionNotFoundError(LedgerError):
    """Raised when transaction ID is not found in database."""
    pass


class ProposalNotFoundError(LedgerError):
    """Raised when pending proposal ID is not found."""
    pass


class DuplicateReversalError(LedgerError):
    """Raised when attempting to reverse an already reversed transaction."""
    pass


class IdempotencyConflictError(LedgerError):
    """Raised when an idempotency key is reused with mismatched parameters."""
    pass


class InvalidTransactionError(LedgerError):
    """Raised when transaction data violates business rules."""
    pass


def _get_customer(conn: sqlite3.Connection, customer_id: int, user_id: Optional[int] = None) -> sqlite3.Row:
    """Fetch customer record or raise CustomerNotFoundError. Validates user_id ownership."""
    if user_id is None:
        user_id = getattr(conn, "current_user_id", None)
    if user_id is None:
        raise ValueError("Merchant user_id is required. Unauthenticated operation is rejected.")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customers WHERE customer_id = ? AND user_id = ?", (customer_id, user_id))
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
    user_id: Optional[int] = None,
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
        cust = _get_customer(conn, customer_id, user_id=user_id)
        resolved_user_id = cust["user_id"]

        # Idempotency check scoped to merchant: if key already exists, verify payload
        if clean_key:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM transactions WHERE user_id = ? AND idempotency_key = ?",
                (resolved_user_id, clean_key),
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
            INSERT INTO transactions (user_id, customer_id, type, amount_paise, item, status, idempotency_key)
            VALUES (?, ?, 'CREDIT', ?, ?, 'COMMITTED', ?)
            """,
            (resolved_user_id, customer_id, amount_paise, clean_item, clean_key),
        )
        tx_id = cursor.lastrowid
        cursor.execute("SELECT * FROM transactions WHERE user_id = ? AND transaction_id = ?", (resolved_user_id, tx_id))
        row = cursor.fetchone()
        return Transaction.from_row(row)


def record_payment(
    conn: sqlite3.Connection,
    customer_id: int,
    amount_paise: int,
    payment_method: Optional[str] = None,
    item: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    user_id: Optional[int] = None,
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
        cust = _get_customer(conn, customer_id, user_id=user_id)
        resolved_user_id = cust["user_id"]

        # Idempotency check scoped to merchant: if key already exists, verify payload
        if clean_key:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM transactions WHERE user_id = ? AND idempotency_key = ?",
                (resolved_user_id, clean_key),
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
            INSERT INTO transactions (user_id, customer_id, type, amount_paise, payment_method, item, status, idempotency_key)
            VALUES (?, ?, 'PAYMENT', ?, ?, ?, 'COMMITTED', ?)
            """,
            (resolved_user_id, customer_id, amount_paise, clean_method, clean_item, clean_key),
        )
        tx_id = cursor.lastrowid
        cursor.execute("SELECT * FROM transactions WHERE user_id = ? AND transaction_id = ?", (resolved_user_id, tx_id))
        row = cursor.fetchone()
        return Transaction.from_row(row)


def get_customer_balance(
    conn: sqlite3.Connection,
    customer_id: int,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Calculate customer's authoritative outstanding balance directly from committed transactions in SQLite.

    Formula:
      Outstanding Balance (Paise) = Total Credits - Total Payments.

    The database is the sole source of truth. Never trusts AI or cached balances.
    """
    customer = _get_customer(conn, customer_id, user_id=user_id)
    resolved_user_id = customer["user_id"]

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type = 'CREDIT' THEN amount_paise ELSE 0 END), 0) AS total_credit_paise,
            COALESCE(SUM(CASE WHEN type = 'PAYMENT' THEN amount_paise ELSE 0 END), 0) AS total_payment_paise,
            COUNT(*) AS transaction_count
        FROM transactions
        WHERE user_id = ? AND customer_id = ?
        """,
        (resolved_user_id, customer_id),
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
    conn: sqlite3.Connection,
    customer_id: int,
    limit: int = 100,
    offset: int = 0,
    user_id: Optional[int] = None,
) -> List[Transaction]:
    """
    Retrieve chronological transaction history for an individual customer.
    All historical entries, reversals, and compensating rows are preserved.
    """
    cust = _get_customer(conn, customer_id, user_id=user_id)
    resolved_user_id = cust["user_id"]

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM transactions
        WHERE user_id = ? AND customer_id = ?
        ORDER BY transaction_date DESC, transaction_id DESC
        LIMIT ? OFFSET ?
        """,
        (resolved_user_id, customer_id, limit, offset),
    )
    rows = cursor.fetchall()
    return [Transaction.from_row(r) for r in rows]


def reverse_transaction(
    conn: sqlite3.Connection,
    transaction_id: int,
    reason: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    user_id: Optional[int] = None,
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

    if user_id is None:
        user_id = getattr(conn, "current_user_id", None)
    if user_id is None:
        raise ValueError("Merchant user_id is required. Unauthenticated operation is rejected.")

    with atomic(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transactions WHERE user_id = ? AND transaction_id = ?", (user_id, transaction_id))
        orig_row = cursor.fetchone()
        if not orig_row:
            raise TransactionNotFoundError(f"Transaction with ID {transaction_id} not found.")

        orig = Transaction.from_row(orig_row)
        resolved_user_id = orig_row["user_id"]

        if orig.reversed_transaction_id is not None:
            raise InvalidTransactionError(
                f"Transaction {transaction_id} is already a compensating reversal and cannot be reversed."
            )

        # Check if already reversed (idempotent replay check)
        cursor.execute(
            "SELECT * FROM transactions WHERE user_id = ? AND reversed_transaction_id = ?",
            (resolved_user_id, transaction_id),
        )
        existing_reversal = cursor.fetchone()
        if existing_reversal:
            comp_tx = Transaction.from_row(existing_reversal)
            bal = get_customer_balance(conn, orig.customer_id, user_id=resolved_user_id)
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
            "UPDATE transactions SET status = 'REVERSED' WHERE user_id = ? AND transaction_id = ?",
            (resolved_user_id, transaction_id),
        )

        try:
            cursor.execute(
                """
                INSERT INTO transactions (
                    user_id, customer_id, type, amount_paise, item, status,
                    reversed_transaction_id, idempotency_key
                )
                VALUES (?, ?, ?, ?, ?, 'COMMITTED', ?, ?)
                """,
                (resolved_user_id, orig.customer_id, comp_type, orig.amount_paise, comp_item, orig.transaction_id, clean_key),
            )
        except sqlite3.IntegrityError as ie:
            if "UNIQUE constraint failed" in str(ie) and "reversed_transaction_id" in str(ie):
                raise DuplicateReversalError(f"Transaction {transaction_id} has already been reversed.") from ie
            raise

        comp_id = cursor.lastrowid
        cursor.execute("SELECT * FROM transactions WHERE user_id = ? AND transaction_id = ?", (resolved_user_id, comp_id))
        comp_row = cursor.fetchone()
        comp_tx = Transaction.from_row(comp_row)

        cursor.execute("SELECT * FROM transactions WHERE user_id = ? AND transaction_id = ?", (resolved_user_id, transaction_id))
        updated_orig_row = cursor.fetchone()
        updated_orig = Transaction.from_row(updated_orig_row)

        bal = get_customer_balance(conn, orig.customer_id, user_id=resolved_user_id)
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
    user_id: Optional[int] = None,
) -> PendingProposal:
    """
    Store an AI transaction proposal awaiting explicit merchant confirmation.
    Does NOT write to the ledger.
    """
    cust = _get_customer(conn, customer_id, user_id=user_id)
    resolved_user_id = cust["user_id"]

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
                proposal_id, user_id, customer_id, intent, amount_paise, item, payment_method, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (proposal_id, resolved_user_id, customer_id, clean_intent, amount_paise, clean_item, clean_method),
        )
        cursor.execute("SELECT * FROM pending_proposals WHERE user_id = ? AND proposal_id = ?", (resolved_user_id, proposal_id))
        row = cursor.fetchone()
        return PendingProposal.from_row(row)


def get_pending_proposal(
    conn: sqlite3.Connection,
    proposal_id: str,
    user_id: Optional[int] = None,
) -> Optional[PendingProposal]:
    """Retrieve pending proposal by proposal_id."""
    if user_id is None:
        user_id = getattr(conn, "current_user_id", None)
    if user_id is None:
        raise ValueError("Merchant user_id is required. Unauthenticated operation is rejected.")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM pending_proposals WHERE user_id = ? AND proposal_id = ?", (user_id, proposal_id))
    row = cursor.fetchone()
    if not row:
        return None
    return PendingProposal.from_row(row)


def confirm_pending_proposal(
    conn: sqlite3.Connection,
    proposal_id: str,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Explicit confirmation: commits a pending proposal into SQLite.
    Idempotent: repeating confirmation for the same proposal_id returns the existing
    committed transaction without duplicating entries.
    """
    with atomic(conn):
        prop = get_pending_proposal(conn, proposal_id, user_id=user_id)
        if not prop:
            raise ProposalNotFoundError(f"Proposal '{proposal_id}' does not exist.")

        cursor = conn.cursor()

        # Idempotent replay: if already confirmed, return existing transaction
        if prop.status == "CONFIRMED" and prop.committed_transaction_id is not None:
            cursor.execute(
                "SELECT * FROM transactions WHERE user_id = ? AND transaction_id = ?",
                (prop.user_id, prop.committed_transaction_id),
            )
            tx_row = cursor.fetchone()
            tx = Transaction.from_row(tx_row)
            bal = get_customer_balance(conn, prop.customer_id, user_id=prop.user_id)
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
                user_id=prop.user_id,
            )
        else:
            tx = record_payment(
                conn,
                customer_id=prop.customer_id,
                amount_paise=prop.amount_paise,
                payment_method=prop.payment_method,
                item=prop.item,
                idempotency_key=prop_key,
                user_id=prop.user_id,
            )

        cursor.execute(
            """
            UPDATE pending_proposals
            SET status = 'CONFIRMED', committed_transaction_id = ?
            WHERE user_id = ? AND proposal_id = ?
            """,
            (tx.transaction_id, prop.user_id, proposal_id),
        )

        bal = get_customer_balance(conn, prop.customer_id, user_id=prop.user_id)
        return {
            "proposal_id": proposal_id,
            "status": "CONFIRMED",
            "committed": True,
            "transaction": tx.to_dict(),
            "customer_balance": bal,
            "message": "Proposal confirmed and committed successfully.",
        }


def cancel_pending_proposal(
    conn: sqlite3.Connection,
    proposal_id: str,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Cancel a pending proposal (e.g. merchant said 'Nahi, rehne do').
    """
    with atomic(conn):
        prop = get_pending_proposal(conn, proposal_id, user_id=user_id)
        if not prop:
            raise ProposalNotFoundError(f"Proposal '{proposal_id}' does not exist.")

        if prop.status == "CONFIRMED":
            raise InvalidTransactionError(f"Proposal '{proposal_id}' has already been confirmed and committed.")

        cursor = conn.cursor()
        cursor.execute(
            "UPDATE pending_proposals SET status = 'CANCELLED' WHERE user_id = ? AND proposal_id = ?",
            (prop.user_id, proposal_id),
        )
        return {
            "proposal_id": proposal_id,
            "status": "CANCELLED",
            "message": "Proposal cancelled successfully.",
        }
