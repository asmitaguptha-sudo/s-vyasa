"""
Pydantic schemas and interface contracts for Vaada Khata.
Strictly enforces:
- Exact 5-field AI-to-Backend JSON contract with extra="forbid".
- Pure Decimal money validation, rejecting NaN, infinity, negatives, and invalid precision.
- Multiple customer aliases.
- Backend-controlled pending proposals and explicit confirmation workflows.
"""

from decimal import Decimal
from enum import Enum
from typing import Any, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.money import parse_rupees_to_paise


class CustomerLookupStatus(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    NOT_FOUND = "NOT_FOUND"


# ---------------------------------------------------------------------------
# Customer Schemas
# ---------------------------------------------------------------------------

class CustomerCreate(BaseModel):
    name: str = Field(..., min_length=1, description="Customer canonical name (e.g. 'Ramesh')")
    alias: Optional[str] = Field(None, description="Primary alias (optional, for backward compatibility)")
    aliases: List[str] = Field(default_factory=list, description="List of customer aliases (e.g. ['Auto wale', 'Auto Ramesh'])")
    phone: Optional[str] = Field(None, description="Optional phone number")

    def get_all_aliases(self) -> List[str]:
        """Combine alias and aliases into a unified list of unique non-empty strings."""
        result: List[str] = []
        if self.alias and self.alias.strip():
            result.append(self.alias.strip())
        for a in self.aliases:
            clean = a.strip() if isinstance(a, str) else ""
            if clean and clean not in result:
                result.append(clean)
        return result


class CustomerResponse(BaseModel):
    customer_id: int
    name: str
    alias: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    phone: Optional[str] = None
    created_at: str

    model_config = ConfigDict(from_attributes=True)


class CustomerLookupResponse(BaseModel):
    status: CustomerLookupStatus = Field(..., description="EXACT_MATCH, AMBIGUOUS_MATCH, or NOT_FOUND")
    query: str = Field(..., description="The queried name or alias")
    customer: Optional[CustomerResponse] = Field(None, description="The resolved customer if exact match")
    candidates: List[CustomerResponse] = Field(default_factory=list, description="Candidate customers if ambiguous")
    clarification_prompt: Optional[str] = Field(
        None, description="Spoken clarification question for shopkeeper if ambiguous (e.g. 'Auto wale Ramesh ya school wale Ramesh?')"
    )


# ---------------------------------------------------------------------------
# Core Ledger Schemas
# ---------------------------------------------------------------------------

class CreditCreate(BaseModel):
    amount: Optional[Any] = Field(None, description="Rupee amount (e.g. 300 or 430.50)")
    amount_paise: Optional[int] = Field(None, description="Exact integer paise (e.g. 30000)")
    item: Optional[str] = Field(None, description="Optional purchase item description")
    idempotency_key: Optional[str] = Field(None, description="Optional idempotency key for network/voice retries")

    def resolve_amount_paise(self) -> int:
        """Resolve to integer paise whether provided in amount or amount_paise."""
        if self.amount_paise is not None:
            if isinstance(self.amount_paise, bool) or not isinstance(self.amount_paise, int) or self.amount_paise <= 0:
                raise ValueError("amount_paise must be an integer greater than zero.")
            return self.amount_paise
        if self.amount is not None:
            return parse_rupees_to_paise(self.amount)
        raise ValueError("Either amount (in rupees) or amount_paise must be provided.")


class PaymentCreate(BaseModel):
    amount: Optional[Any] = Field(None, description="Rupee amount (e.g. 200 or 500)")
    amount_paise: Optional[int] = Field(None, description="Exact integer paise (e.g. 20000)")
    payment_method: Optional[str] = Field(None, description="Payment method: CASH, UPI, or OTHER")
    item: Optional[str] = Field(None, description="Optional payment notes")
    idempotency_key: Optional[str] = Field(None, description="Optional idempotency key for network/voice retries")

    def resolve_amount_paise(self) -> int:
        """Resolve to integer paise whether provided in amount or amount_paise."""
        if self.amount_paise is not None:
            if isinstance(self.amount_paise, bool) or not isinstance(self.amount_paise, int) or self.amount_paise <= 0:
                raise ValueError("amount_paise must be an integer greater than zero.")
            return self.amount_paise
        if self.amount is not None:
            return parse_rupees_to_paise(self.amount)
        raise ValueError("Either amount (in rupees) or amount_paise must be provided.")


class TransactionResponse(BaseModel):
    transaction_id: int
    customer_id: int
    type: str  # CREDIT or PAYMENT
    amount_paise: int
    amount_rupees: float
    item: Optional[str] = None
    payment_method: Optional[str] = None
    transaction_date: Optional[str] = None
    status: str  # COMMITTED or REVERSED
    reversed_transaction_id: Optional[int] = None
    idempotency_key: Optional[str] = None
    created_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class CustomerBalanceResponse(BaseModel):
    customer_id: int
    customer_name: str
    balance_paise: int
    balance_rupees: float
    total_credit_paise: int
    total_credit_rupees: float
    total_payment_paise: int
    total_payment_rupees: float
    transaction_count: int


class ReversalCreate(BaseModel):
    reason: Optional[str] = Field(None, description="Reason for reversal (e.g. 'Merchant corrected amount')")
    idempotency_key: Optional[str] = Field(None, description="Optional idempotency key")


class ReversalResponse(BaseModel):
    reversed_transaction: TransactionResponse
    compensating_transaction: TransactionResponse
    balance_paise: int
    balance_rupees: float
    message: str


# ---------------------------------------------------------------------------
# Strict AI-to-Backend JSON Contract
# Required exact 5-field structure for AI/voice layer integration.
# Do not rename or remove any fields. Extra fields are strictly forbidden.
# ---------------------------------------------------------------------------

class AiTransactionProposal(BaseModel):
    """
    Exact AI-to-Backend JSON Contract:
    {
      "intent": "add_credit",
      "customer_name": "Ramesh",
      "amount": 300,
      "item": null,
      "confirmation_required": true
    }
    """
    intent: str = Field(..., min_length=1, description="Action intent, e.g. 'add_credit', 'record_payment'")
    customer_name: str = Field(..., min_length=1, description="Customer name extracted by speech/AI layer")
    amount: Any = Field(..., description="Numeric amount in rupees (e.g. 300, 430.50, or '19.99')")
    item: Optional[str] = Field(..., description="Item purchased or null")
    confirmation_required: bool = Field(..., description="Whether confirmation is required before commit")

    # Strictly forbid unexpected extra fields
    model_config = ConfigDict(extra="forbid")

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: Any) -> Decimal:
        """
        Validates that amount is numeric, finite, non-negative, and has at most 2 decimal places.
        Rejects booleans, NaN, Infinity, negative values, and fractional paise.
        """
        if isinstance(v, bool):
            raise ValueError("Amount cannot be a boolean value.")
        parse_rupees_to_paise(v)
        return Decimal(str(v).strip())

    @property
    def amount_paise(self) -> int:
        """Helper to convert rupee amount to integer paise safely without floating-point inaccuracy."""
        return parse_rupees_to_paise(self.amount)


# ---------------------------------------------------------------------------
# Backend-Controlled Proposal & Confirmation Schemas
# ---------------------------------------------------------------------------

class ProposalResponse(BaseModel):
    proposal_id: str
    status: str  # PENDING_CONFIRMATION, CONFIRMED, CANCELLED
    customer_id: int
    customer_name: str
    intent: str
    amount_paise: int
    amount_rupees: float
    item: Optional[str] = None
    confirmation_prompt: str
    confirmation_required: bool
    committed: bool


class ProposalConfirmResponse(BaseModel):
    proposal_id: str
    status: str
    committed: bool
    transaction: TransactionResponse
    customer_balance: CustomerBalanceResponse
    message: str
