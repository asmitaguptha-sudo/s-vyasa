# Vaada Khata --- Voice Khata for Kirana Stores
**Team:** AuraX | **Institution:** RV University, Bengaluru
**Hackathon:** Gnani Voice Hackathon | **Track:** PS2 --- Voice Khata for Kirana Stores
**Current Branch:** `feature/ledger-backend`

---

## 1. Project Overview & Scope

**Vaada Khata** is a voice-first conversational digital ledger for Indian kirana shopkeepers. It turns natural counter speech into validated ledger entries and remembers both what customers owe (*udhaar*) and what they promise to pay (*vaada*).

### Milestone 1 & 2 Completed Architecture:
1. **SQLite Database Connection & Migrations**: WAL mode, foreign key enforcement, and safe idempotent schema migrations.
2. **Normalized Customers & Multiple Aliases**:
   - `customers`: Canonical name and phone records.
   - `customer_aliases`: Normalized secondary table supporting multiple shopkeeper nicknames (e.g., *Auto wale*, *Auto driver*, *DL 4567*).
   - Duplicate-name disambiguation with natural spoken clarification prompts (*"Auto wale Ramesh ya school wale Ramesh?"*).
3. **Core Ledger Engine (`backend/ledger.py`)**:
   - **Add Credit**: Records customer udhaar, increases outstanding balance.
   - **Record Payment**: Records customer payments (CASH, UPI, OTHER), decreases outstanding balance.
   - **Authoritative Balance**: Computes balance strictly from SQLite: $\sum(\text{CREDIT}) - \sum(\text{PAYMENT})$. Never trusts AI-generated balances.
   - **Historical Integrity**: Preserves chronological transaction history with audit details.
   - **Atomic Writes & Idempotency**: `BEGIN IMMEDIATE` write reservations; payload-bound idempotency prevents duplicate requests.
   - **Safe Additive Reversals**: Corrections append compensating entries, mark original as `REVERSED`, and link `reversed_transaction_id` (enforced as UNIQUE to prevent double reversals).
4. **AI-to-Backend JSON Contract Gate**:
   - Strictly preserves the five-field AI contract: `intent`, `customer_name`, `amount`, `item`, `confirmation_required`.
   - Prevents premature ledger writes while `confirmation_required` is `true`. Only commits when confirmed (`confirmation_required == false`).
   - Exact Decimal-to-integer-paise conversion (₹19.99 $\to$ 1999 paise; ₹430.50 $\to$ 43050 paise).

---

## 2. API Endpoints Reference

### Customer Management
- `POST /customers`: Register new customer with optional aliases.
- `GET /customers`: List customers (paginated).
- `GET /customers/lookup?q=...`: Search customer by name or any alias with duplicate resolution.
- `GET /customers/{customer_id}`: Retrieve customer details.

### Core Ledger Operations
- `POST /customers/{customer_id}/credit`: Record customer credit (Udhaar).
- `POST /customers/{customer_id}/payment`: Record customer payment (CASH, UPI, OTHER).
- `GET /customers/{customer_id}/balance`: Get customer's authoritative outstanding balance.
- `GET /customers/{customer_id}/transactions`: Get customer transaction history (paginated).
- `POST /transactions/{transaction_id}/reverse`: Additive compensating reversal of an earlier transaction.

### AI Interface Contract & Confirmation Workflow
- `POST /ai/contract/propose`: Ingest AI contract payload, resolve customer lookup, and generate a backend-controlled pending proposal in SQLite with a Hinglish confirmation prompt (no ledger writes).
- `POST /ai/proposals/{proposal_id}/confirm`: Explicit confirmation gate. Commits pending proposal into ledger atomically. Idempotent on repeated calls.
- `POST /ai/proposals/{proposal_id}/cancel`: Cancel a pending proposal if merchant declines.
- `POST /ai/contract/validate`: Validate AI proposal and resolve customer lookup without writing to database.
- `POST /ai/contract/commit`: Legacy direct AI commit gate (rejects if `confirmation_required: true`).

---

## 3. Example Requests & Responses

### A. Record Credit (Udhaar)
**Endpoint:** `POST /customers/1/credit`
```json
// Request
{
  "amount": 340.50,
  "item": "Rice 5kg & Cooking Oil",
  "idempotency_key": "voice-req-001"
}

// Response (201 Created)
{
  "transaction_id": 1,
  "customer_id": 1,
  "type": "CREDIT",
  "amount_paise": 34050,
  "amount_rupees": 340.50,
  "item": "Rice 5kg & Cooking Oil",
  "payment_method": null,
  "transaction_date": "2026-11-21 14:00:00",
  "status": "COMMITTED",
  "reversed_transaction_id": null,
  "idempotency_key": "voice-req-001",
  "created_at": "2026-11-21 14:00:00"
}
```

### B. Record Payment
**Endpoint:** `POST /customers/1/payment`
```json
// Request
{
  "amount": 200,
  "payment_method": "UPI",
  "item": "Google Pay transfer"
}

// Response (201 Created)
{
  "transaction_id": 2,
  "customer_id": 1,
  "type": "PAYMENT",
  "amount_paise": 20000,
  "amount_rupees": 200.0,
  "item": "Google Pay transfer",
  "payment_method": "UPI",
  "transaction_date": "2026-11-21 14:05:00",
  "status": "COMMITTED",
  "reversed_transaction_id": null,
  "idempotency_key": null,
  "created_at": "2026-11-21 14:05:00"
}
```

### C. Authoritative Outstanding Balance
**Endpoint:** `GET /customers/1/balance`
```json
// Response (200 OK)
{
  "customer_id": 1,
  "customer_name": "Ramesh",
  "balance_paise": 14050,
  "balance_rupees": 140.50,
  "total_credit_paise": 34050,
  "total_credit_rupees": 340.50,
  "total_payment_paise": 20000,
  "total_payment_rupees": 200.0,
  "transaction_count": 2
}
```

### D. Compensating Reversal
**Endpoint:** `POST /transactions/1/reverse`
```json
// Request
{
  "reason": "Shopkeeper entered ₹340.50 instead of ₹240.50"
}

// Response (200 OK)
{
  "reversed_transaction": {
    "transaction_id": 1,
    "status": "REVERSED",
    "amount_paise": 34050
  },
  "compensating_transaction": {
    "transaction_id": 3,
    "customer_id": 1,
    "type": "PAYMENT",
    "amount_paise": 34050,
    "status": "COMMITTED",
    "reversed_transaction_id": 1,
    "item": "Reversal of transaction #1: Shopkeeper entered ₹340.50 instead of ₹240.50"
  },
  "balance_paise": -20000,
  "balance_rupees": -200.0,
  "message": "Transaction 1 reversed successfully."
}
```

### E. AI Contract & Backend-Controlled Confirmation Workflow

#### 1. Propose Intent (`POST /ai/contract/propose`)
Incoming contract strictly validated (`extra="forbid"`, exact 5 fields). Resolves customer lookup and creates a `PENDING` proposal record in SQLite.
```json
// Request from Speech Layer
{
  "intent": "add_credit",
  "customer_name": "Ramesh",
  "amount": 300,
  "item": "Wheat flour",
  "confirmation_required": true
}

// Response (200 OK - Staged in pending_proposals, no ledger write)
{
  "proposal_id": "7b31c51c-3cb8-4a64-9694-850ecf3cbef1",
  "status": "PENDING_CONFIRMATION",
  "customer_id": 1,
  "customer_name": "Ramesh",
  "intent": "add_credit",
  "amount_paise": 30000,
  "amount_rupees": 300.0,
  "item": "Wheat flour",
  "confirmation_prompt": "Ramesh ke ₹300.00 add kar doon?",
  "confirmation_required": true,
  "committed": false
}
```

#### 2. Confirm Proposal (`POST /ai/proposals/{proposal_id}/confirm`)
Triggered when the merchant says *"Haan"* or taps confirm. Idempotent: repeated calls safely return the already-committed transaction.
```json
// Response (200 OK - Atomically written to ledger)
{
  "proposal_id": "7b31c51c-3cb8-4a64-9694-850ecf3cbef1",
  "status": "CONFIRMED",
  "committed": true,
  "transaction": {
    "transaction_id": 4,
    "customer_id": 1,
    "type": "CREDIT",
    "amount_paise": 30000,
    "amount_rupees": 300.0,
    "status": "COMMITTED"
  },
  "customer_balance": {
    "customer_id": 1,
    "balance_paise": 30000,
    "balance_rupees": 300.0
  }
}
```

#### 3. Cancel Proposal (`POST /ai/proposals/{proposal_id}/cancel`)
Triggered if the merchant declines (*"Nahi"*). Marks the proposal `CANCELLED` so it can never be confirmed.

---

## 4. Setup and Installation

### Setup Virtual Environment
```powershell
cd "C:\Users\Asmita Guptha\s-vyasa"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## 5. Running the Backend Server
```powershell
uvicorn backend.main:app --reload --port 8000
```
- API Base URL: `http://127.0.0.1:8000`
- Interactive Swagger Documentation: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/health`

---

## 6. Running Tests
```powershell
pytest -v
```
All 70 comprehensive unit, database, migration, and API tests pass in ~0.9s.
