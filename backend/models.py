"""
Domain models and database entity representations for Vaada Khata.
"""

from dataclasses import dataclass, field
from typing import List, Optional
import sqlite3


@dataclass
class CustomerAlias:
    alias_id: int
    customer_id: int
    alias: str
    user_id: Optional[int] = None
    created_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CustomerAlias":
        return cls(
            alias_id=row["alias_id"],
            customer_id=row["customer_id"],
            alias=row["alias"],
            user_id=row["user_id"] if "user_id" in row.keys() else None,
            created_at=row["created_at"] if "created_at" in row.keys() else None,
        )


@dataclass
class Customer:
    customer_id: int
    name: str
    user_id: Optional[int] = None
    phone: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    created_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row, aliases: Optional[List[str]] = None) -> "Customer":
        return cls(
            customer_id=row["customer_id"],
            name=row["name"],
            user_id=row["user_id"] if "user_id" in row.keys() else None,
            phone=row["phone"] if "phone" in row.keys() else None,
            aliases=aliases if aliases is not None else [],
            created_at=row["created_at"] if "created_at" in row.keys() else None,
        )

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "name": self.name,
            "alias": self.aliases[0] if self.aliases else None,
            "aliases": self.aliases,
            "phone": self.phone,
            "created_at": self.created_at,
        }


@dataclass
class Transaction:
    transaction_id: int
    customer_id: int
    type: str  # CREDIT or PAYMENT
    amount_paise: int
    user_id: Optional[int] = None
    item: Optional[str] = None
    payment_method: Optional[str] = None  # CASH, UPI, OTHER, or None
    transaction_date: Optional[str] = None
    status: str = "COMMITTED"
    reversed_transaction_id: Optional[int] = None
    idempotency_key: Optional[str] = None
    created_at: Optional[str] = None

    @property
    def amount_rupees(self) -> float:
        return self.amount_paise / 100.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Transaction":
        return cls(
            transaction_id=row["transaction_id"],
            customer_id=row["customer_id"],
            type=row["type"],
            amount_paise=row["amount_paise"],
            user_id=row["user_id"] if "user_id" in row.keys() else None,
            item=row["item"] if "item" in row.keys() else None,
            payment_method=row["payment_method"] if "payment_method" in row.keys() else None,
            transaction_date=row["transaction_date"] if "transaction_date" in row.keys() else None,
            status=row["status"] if "status" in row.keys() else "COMMITTED",
            reversed_transaction_id=row["reversed_transaction_id"] if "reversed_transaction_id" in row.keys() else None,
            idempotency_key=row["idempotency_key"] if "idempotency_key" in row.keys() else None,
            created_at=row["created_at"] if "created_at" in row.keys() else None,
        )

    def to_dict(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "customer_id": self.customer_id,
            "type": self.type,
            "amount_paise": self.amount_paise,
            "amount_rupees": self.amount_paise / 100.0,
            "item": self.item,
            "payment_method": self.payment_method,
            "transaction_date": self.transaction_date,
            "status": self.status,
            "reversed_transaction_id": self.reversed_transaction_id,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
        }


@dataclass
class Promise:
    promise_id: int
    customer_id: int
    amount_paise: int
    due_date: str
    user_id: Optional[int] = None
    status: str = "PENDING"
    fulfilled_by_transaction_id: Optional[int] = None
    created_at: Optional[str] = None

    @property
    def amount_rupees(self) -> float:
        return self.amount_paise / 100.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Promise":
        return cls(
            promise_id=row["promise_id"],
            customer_id=row["customer_id"],
            amount_paise=row["amount_paise"],
            due_date=row["due_date"],
            user_id=row["user_id"] if "user_id" in row.keys() else None,
            status=row["status"] if "status" in row.keys() else "PENDING",
            fulfilled_by_transaction_id=row["fulfilled_by_transaction_id"] if "fulfilled_by_transaction_id" in row.keys() else None,
            created_at=row["created_at"] if "created_at" in row.keys() else None,
        )

    def to_dict(self) -> dict:
        return {
            "promise_id": self.promise_id,
            "customer_id": self.customer_id,
            "amount_paise": self.amount_paise,
            "amount_rupees": self.amount_paise / 100.0,
            "due_date": self.due_date,
            "status": self.status,
            "fulfilled_by_transaction_id": self.fulfilled_by_transaction_id,
            "created_at": self.created_at,
        }


@dataclass
class PendingProposal:
    proposal_id: str
    customer_id: int
    intent: str
    amount_paise: int
    user_id: Optional[int] = None
    item: Optional[str] = None
    payment_method: Optional[str] = None
    status: str = "PENDING"  # PENDING, CONFIRMED, CANCELLED
    committed_transaction_id: Optional[int] = None
    created_at: Optional[str] = None

    @property
    def amount_rupees(self) -> float:
        return self.amount_paise / 100.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PendingProposal":
        return cls(
            proposal_id=row["proposal_id"],
            customer_id=row["customer_id"],
            intent=row["intent"],
            amount_paise=row["amount_paise"],
            user_id=row["user_id"] if "user_id" in row.keys() else None,
            item=row["item"] if "item" in row.keys() else None,
            payment_method=row["payment_method"] if "payment_method" in row.keys() else None,
            status=row["status"] if "status" in row.keys() else "PENDING",
            committed_transaction_id=row["committed_transaction_id"] if "committed_transaction_id" in row.keys() else None,
            created_at=row["created_at"] if "created_at" in row.keys() else None,
        )

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "customer_id": self.customer_id,
            "intent": self.intent,
            "amount_paise": self.amount_paise,
            "amount_rupees": self.amount_paise / 100.0,
            "item": self.item,
            "payment_method": self.payment_method,
            "status": self.status,
            "committed_transaction_id": self.committed_transaction_id,
            "created_at": self.created_at,
        }
