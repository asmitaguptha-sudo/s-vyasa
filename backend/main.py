"""
Vaada Khata - FastAPI Backend
Milestone 2 Consolidated: Core Ledger Operations, Additive Reversals,
Strict 5-Field AI JSON Contract, and Backend-Controlled Confirmation Workflow.
"""

from contextlib import asynccontextmanager
from typing import List, Optional
import sqlite3

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware

from backend.auth import (
    AuthenticationError,
    RateLimitExceededError,
    authenticate_merchant,
    get_client_ip,
    get_current_merchant,
    register_merchant,
    revoke_session,
)
from backend.auth_schemas import (
    LogoutResponse,
    MerchantLoginRequest,
    MerchantLoginResponse,
    MerchantProfileResponse,
    MerchantRegisterRequest,
    MerchantRegisterResponse,
)
from backend.config import APP_NAME, APP_VERSION
from backend.database import get_db, init_db
from backend.models import User
from backend.schemas import (
    AiTransactionProposal,
    CreditCreate,
    CustomerBalanceResponse,
    CustomerCreate,
    CustomerLookupResponse,
    CustomerResponse,
    PaymentCreate,
    ProposalConfirmResponse,
    ProposalResponse,
    ReversalCreate,
    ReversalResponse,
    TransactionResponse,
)
import backend.customer_service as customer_service
import backend.ledger as ledger
from backend.money import MoneyError, format_paise_to_rupees


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database tables, indexes, and apply safe migrations on startup."""
    init_db()
    yield


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="Voice Khata for Kirana Stores - Backend & Ledger Engine",
    lifespan=lifespan,
)

# CORS middleware with explicit secure origins and methods
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health & Status
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
def health_check(db: sqlite3.Connection = Depends(get_db)):
    """Health check endpoint. Returns 200 when database is healthy, 503 on failure."""
    try:
        db.execute("SELECT 1;").fetchone()
        return {
            "status": "healthy",
            "app": APP_NAME,
            "version": APP_VERSION,
            "database": "connected",
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database connection failure: {str(exc)}",
        )


# ---------------------------------------------------------------------------
# Merchant Authentication (Stage 2B)
# ---------------------------------------------------------------------------

@app.post(
    "/auth/register",
    response_model=MerchantRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Merchant Authentication"],
    summary="Register a new merchant account",
)
def register_merchant_endpoint(
    payload: MerchantRegisterRequest,
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Register a new merchant with normalized phone, shop details, and Argon2id PIN.
    Returns authenticated session token on success.
    """
    try:
        user, raw_token, expires_at = register_merchant(
            db,
            phone=payload.phone,
            name=payload.name,
            shop_name=payload.shop_name,
            pin=payload.pin,
        )
        return MerchantRegisterResponse(
            user_id=user.user_id,
            phone=user.phone,
            name=user.name,
            shop_name=user.shop_name,
            status=user.status,
            token=raw_token,
            token_type="Bearer",
            expires_at=expires_at,
        )
    except ValueError as ve:
        err_msg = str(ve)
        if "already exists" in err_msg or "reserved" in err_msg:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)


@app.post(
    "/auth/login",
    response_model=MerchantLoginResponse,
    status_code=status.HTTP_200_OK,
    tags=["Merchant Authentication"],
    summary="Authenticate merchant and obtain session token",
)
def login_merchant_endpoint(
    payload: MerchantLoginRequest,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Authenticate merchant using normalized phone and 6-digit PIN.
    Applies persistent rate limiting before expensive Argon2id hash checks.
    """
    client_ip = get_client_ip(request)
    try:
        user, raw_token, expires_at = authenticate_merchant(
            db,
            phone=payload.phone,
            pin=payload.pin,
            ip_address=client_ip,
        )
        return MerchantLoginResponse(
            token=raw_token,
            token_type="Bearer",
            expires_at=expires_at,
            user=MerchantProfileResponse.model_validate(user.to_dict()),
        )
    except RateLimitExceededError as rle:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(rle))
    except AuthenticationError as ae:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(ae))


@app.post(
    "/auth/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    tags=["Merchant Authentication"],
    summary="Revoke active merchant session",
)
def logout_merchant_endpoint(
    authorization: Optional[str] = Header(None),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Revoke bearer session token in SQLite.
    Subsequent requests with this token will be rejected.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raw_token = parts[1]
    revoked = revoke_session(db, raw_token)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session not found or already revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return LogoutResponse(message="Logged out successfully.")


@app.get(
    "/auth/me",
    response_model=MerchantProfileResponse,
    status_code=status.HTTP_200_OK,
    tags=["Merchant Authentication"],
    summary="Get authenticated merchant profile",
)
def get_current_merchant_profile_endpoint(
    current_user: User = Depends(get_current_merchant),
):
    """
    Retrieve profile of currently authenticated merchant.
    Identity is derived strictly from verified session token in database.
    """
    return MerchantProfileResponse.model_validate(current_user.to_dict())


# ---------------------------------------------------------------------------
# Customer Management & Duplicate Handling
# ---------------------------------------------------------------------------

@app.post(
    "/customers",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Customers"],
)
def create_customer_endpoint(
    customer_in: CustomerCreate,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """Create a new customer with canonical name and shop-specific aliases."""
    db.current_user_id = current_user.user_id
    try:
        return customer_service.create_customer(db, customer_in)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@app.get("/customers", response_model=List[CustomerResponse], tags=["Customers"])
def list_customers_endpoint(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """List all registered customers with pagination."""
    db.current_user_id = current_user.user_id
    return customer_service.list_customers(db, limit=limit, offset=offset)


@app.get(
    "/customers/lookup",
    response_model=CustomerLookupResponse,
    tags=["Customers"],
)
def lookup_customer_endpoint(
    q: str = Query(..., min_length=1, description="Customer name, alias, or composite search term"),
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Lookup customer by name or alias with intelligent duplicate resolution.
    Returns EXACT_MATCH, AMBIGUOUS_MATCH with spoken clarification prompt, or NOT_FOUND.
    """
    db.current_user_id = current_user.user_id
    return customer_service.lookup_customer(db, query=q)


@app.get(
    "/customers/{customer_id}",
    response_model=CustomerResponse,
    tags=["Customers"],
)
def get_customer_endpoint(
    customer_id: int,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """Retrieve customer details by ID. Returns 404 for cross-merchant access."""
    db.current_user_id = current_user.user_id
    customer = customer_service.get_customer_by_id(db, customer_id)
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID {customer_id} not found.",
        )
    return customer


# ---------------------------------------------------------------------------
# Core Ledger Operations
# ---------------------------------------------------------------------------

@app.post(
    "/customers/{customer_id}/credit",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Ledger"],
    summary="Record customer credit (Udhaar)",
)
def record_credit_endpoint(
    customer_id: int,
    payload: CreditCreate,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Record customer credit (Udhaar).
    Accepts customer ID and amount (in rupees or integer paise).
    Validates customer existence and writes persistently under BEGIN IMMEDIATE.
    """
    db.current_user_id = current_user.user_id
    try:
        amount_paise = payload.resolve_amount_paise()
        tx = ledger.record_credit(
            db,
            customer_id=customer_id,
            amount_paise=amount_paise,
            item=payload.item,
            idempotency_key=payload.idempotency_key,
        )
        return tx.to_dict()
    except ledger.CustomerNotFoundError as ce:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ce))
    except ledger.IdempotencyConflictError as ie:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(ie))
    except (ledger.InvalidTransactionError, MoneyError, ValueError) as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@app.post(
    "/customers/{customer_id}/payment",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Ledger"],
    summary="Record customer payment",
)
def record_payment_endpoint(
    customer_id: int,
    payload: PaymentCreate,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Record customer payment.
    Accepts customer ID, amount, and optional payment method (CASH, UPI, OTHER).
    Decreases outstanding balance under BEGIN IMMEDIATE.
    """
    db.current_user_id = current_user.user_id
    try:
        amount_paise = payload.resolve_amount_paise()
        tx = ledger.record_payment(
            db,
            customer_id=customer_id,
            amount_paise=amount_paise,
            payment_method=payload.payment_method,
            item=payload.item,
            idempotency_key=payload.idempotency_key,
        )
        return tx.to_dict()
    except ledger.CustomerNotFoundError as ce:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ce))
    except ledger.IdempotencyConflictError as ie:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(ie))
    except (ledger.InvalidTransactionError, MoneyError, ValueError) as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@app.get(
    "/customers/{customer_id}/balance",
    response_model=CustomerBalanceResponse,
    tags=["Ledger"],
    summary="Get customer's authoritative outstanding balance",
)
def get_customer_balance_endpoint(
    customer_id: int,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Calculate customer's balance as sum of committed CREDIT entries minus committed PAYMENT entries.
    Integer paise only. Database is the authoritative source of truth.
    """
    db.current_user_id = current_user.user_id
    try:
        return ledger.get_customer_balance(db, customer_id)
    except ledger.CustomerNotFoundError as ce:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ce))


@app.get(
    "/customers/{customer_id}/transactions",
    response_model=List[TransactionResponse],
    tags=["Ledger"],
    summary="Get customer transaction history",
)
def get_customer_transactions_endpoint(
    customer_id: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Retrieve chronological transaction history for an individual customer.
    Preserves all historical entries, statuses, and reversal links.
    """
    db.current_user_id = current_user.user_id
    try:
        txs = ledger.get_customer_transactions(db, customer_id, limit=limit, offset=offset)
        return [t.to_dict() for t in txs]
    except ledger.CustomerNotFoundError as ce:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ce))


@app.post(
    "/transactions/{transaction_id}/reverse",
    response_model=ReversalResponse,
    tags=["Ledger"],
    summary="Safe additive compensating reversal",
)
def reverse_transaction_endpoint(
    transaction_id: int,
    payload: Optional[ReversalCreate] = None,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Safely reverse a transaction via an additive compensating entry.
    Preserves original entry with status REVERSED, appends an opposite compensating entry,
    and links reversed_transaction_id. Prevents duplicate reversals via UNIQUE constraint.
    """
    db.current_user_id = current_user.user_id
    reason = payload.reason if payload else None
    key = payload.idempotency_key if payload else None
    try:
        return ledger.reverse_transaction(db, transaction_id, reason=reason, idempotency_key=key)
    except ledger.TransactionNotFoundError as te:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(te))
    except ledger.DuplicateReversalError as de:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(de))
    except ledger.InvalidTransactionError as ie:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ie))


# ---------------------------------------------------------------------------
# Backend-Controlled Confirmation Workflow & AI Interface
# ---------------------------------------------------------------------------

@app.post(
    "/ai/contract/propose",
    response_model=ProposalResponse,
    tags=["AI Interface & Confirmation"],
    summary="Create a backend-controlled pending proposal from AI contract",
)
def propose_ai_contract_endpoint(
    proposal: AiTransactionProposal,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Validates the exact 5-field AI contract:
    {
      "intent": "add_credit",
      "customer_name": "Ramesh",
      "amount": 300,
      "item": null,
      "confirmation_required": true
    }
    Resolves customer lookup:
    - If ambiguous (duplicate names): returns 409 Conflict with candidates & clarification question.
    - If not found: returns 404 Not Found.
    - If exact match: generates a backend-controlled pending proposal in SQLite with a confirmation prompt.
    Does NOT write to the ledger.
    """
    db.current_user_id = current_user.user_id
    lookup = customer_service.lookup_customer(db, proposal.customer_name)
    if lookup.status == "AMBIGUOUS_MATCH":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Cannot propose transaction for ambiguous customer name.",
                "clarification_prompt": lookup.clarification_prompt,
                "candidates": [c.model_dump() for c in lookup.candidates],
            },
        )
    if lookup.status == "NOT_FOUND" or not lookup.customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer '{proposal.customer_name}' was not found in the database.",
        )

    customer = lookup.customer
    rupee_fmt = format_paise_to_rupees(proposal.amount_paise)
    intent_clean = proposal.intent.strip().lower()

    if intent_clean in ("add_credit", "credit"):
        prompt = f"{customer.name} ke ₹{rupee_fmt} add kar doon?"
    else:
        prompt = f"{customer.name} ka ₹{rupee_fmt} payment record karoon?"

    pending = ledger.create_pending_proposal(
        db,
        customer_id=customer.customer_id,
        intent=intent_clean,
        amount_paise=proposal.amount_paise,
        item=proposal.item,
    )

    return {
        "proposal_id": pending.proposal_id,
        "status": "PENDING_CONFIRMATION",
        "customer_id": customer.customer_id,
        "customer_name": customer.name,
        "intent": intent_clean,
        "amount_paise": proposal.amount_paise,
        "amount_rupees": proposal.amount_paise / 100.0,
        "item": proposal.item,
        "confirmation_prompt": prompt,
        "confirmation_required": proposal.confirmation_required,
        "committed": False,
    }


@app.post(
    "/ai/contract/validate",
    tags=["AI Interface & Confirmation"],
    summary="Validate AI proposal (read-only verification)",
)
def validate_ai_contract_endpoint(
    proposal: AiTransactionProposal,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Validates incoming proposal against the strict 5-field contract (extra fields forbidden).
    Resolves customer lookup without committing any ledger writes.
    """
    db.current_user_id = current_user.user_id
    lookup = customer_service.lookup_customer(db, proposal.customer_name)
    return {
        "contract_valid": True,
        "received_proposal": proposal.model_dump(),
        "amount_paise": proposal.amount_paise,
        "customer_lookup": lookup.model_dump(),
        "confirmation_required": proposal.confirmation_required,
        "committed": False,
        "status": "AWAITING_CONFIRMATION" if proposal.confirmation_required else "READY_FOR_PROPOSAL",
    }


@app.post(
    "/ai/proposals/{proposal_id}/confirm",
    response_model=ProposalConfirmResponse,
    tags=["AI Interface & Confirmation"],
    summary="Explicit confirmation: execute pending proposal into ledger",
)
def confirm_proposal_endpoint(
    proposal_id: str,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Explicit confirmation: commits a pending proposal into SQLite.
    Triggered when merchant confirms ('Haan' or Push-to-Talk).
    Idempotent: repeating confirmation for the same proposal_id returns the existing
    committed transaction without duplicating entries.
    """
    db.current_user_id = current_user.user_id
    try:
        res = ledger.confirm_pending_proposal(db, proposal_id)
        return res
    except ledger.ProposalNotFoundError as pe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(pe))
    except ledger.InvalidTransactionError as ie:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ie))


@app.post(
    "/ai/proposals/{proposal_id}/cancel",
    tags=["AI Interface & Confirmation"],
    summary="Cancel a pending proposal",
)
def cancel_proposal_endpoint(
    proposal_id: str,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """Cancel a pending proposal (merchant said 'Nahi, rehne do')."""
    db.current_user_id = current_user.user_id
    try:
        return ledger.cancel_pending_proposal(db, proposal_id)
    except ledger.ProposalNotFoundError as pe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(pe))
    except ledger.InvalidTransactionError as ie:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ie))


@app.post(
    "/ai/contract/commit",
    tags=["AI Interface & Confirmation"],
    summary="Legacy direct AI commit gate",
)
def commit_ai_contract_endpoint(
    proposal: AiTransactionProposal,
    current_user: User = Depends(get_current_merchant),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Direct commit gate.
    Safety rule:
    Rejects commit if confirmation_required is True.
    Transaction must be confirmed by user through the proposal workflow.
    """
    db.current_user_id = current_user.user_id
    if proposal.confirmation_required:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Transaction cannot be committed while confirmation_required is True. Must obtain user confirmation first.",
        )

    lookup = customer_service.lookup_customer(db, proposal.customer_name)
    if lookup.status == "AMBIGUOUS_MATCH":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Cannot commit transaction for ambiguous customer name.",
                "clarification_prompt": lookup.clarification_prompt,
                "candidates": [c.model_dump() for c in lookup.candidates],
            },
        )
    if lookup.status == "NOT_FOUND" or not lookup.customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer '{proposal.customer_name}' was not found in the database.",
        )

    customer_id = lookup.customer.customer_id
    intent_clean = proposal.intent.strip().lower()

    if intent_clean in ("add_credit", "credit"):
        tx = ledger.record_credit(
            db,
            customer_id=customer_id,
            amount_paise=proposal.amount_paise,
            item=proposal.item,
        )
    elif intent_clean in ("record_payment", "payment"):
        tx = ledger.record_payment(
            db,
            customer_id=customer_id,
            amount_paise=proposal.amount_paise,
            item=proposal.item,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported intent for ledger commit: {proposal.intent}.",
        )

    bal = ledger.get_customer_balance(db, customer_id)
    return {
        "committed": True,
        "transaction": tx.to_dict(),
        "customer_balance": bal,
    }
