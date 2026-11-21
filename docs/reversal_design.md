# Additive Reversal Architecture & Financial Safety Design
**Project:** Vaada Khata (Voice Khata for Kirana Stores)
**Reference Model:** Inspired by `xdcte5/Ritsus` (MIT)

---

## 1. Architectural Principles

In a voice-first Kirana ledger, shopkeepers often speak quickly and make corrections (e.g., *"Nahi, 430"* before confirmation, or discovering a mistake on an earlier entry).

To guarantee auditable, tamper-proof accounting:
1. **No Silent In-Place Updates:** Never overwrite or mutate historical financial amounts or transaction types.
2. **No Physical Deletions:** Committed transactions are immutable and must never be deleted.
3. **Additive Compensating Transactions:** Corrections append a distinct offsetting entry that neutralizes the financial effect of the erroneous entry.
4. **Explicit Lineage:** The compensating transaction explicitly links back to the original via `reversed_transaction_id`.
5. **Idempotent Single-Reversal:** A transaction can only be reversed **once**. Double reversals are blocked by database constraints.

---

## 2. Relational Schema Design

```sql
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('CREDIT', 'PAYMENT')),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    item TEXT,
    payment_method TEXT CHECK (payment_method IN ('CASH', 'UPI', 'OTHER') OR payment_method IS NULL),
    transaction_date TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    status TEXT NOT NULL DEFAULT 'COMMITTED' CHECK (status IN ('COMMITTED', 'REVERSED')),
    reversed_transaction_id INTEGER UNIQUE,
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE RESTRICT,
    FOREIGN KEY (reversed_transaction_id) REFERENCES transactions(transaction_id) ON DELETE RESTRICT
);
```

### Key Safety Invariants:
- `reversed_transaction_id INTEGER UNIQUE`: Ensures that at most **one** compensating reversal transaction can ever reference an original transaction ID. Any duplicate reversal attempt causes a SQLite unique constraint violation (`409 Conflict`).
- `FOREIGN KEY (reversed_transaction_id) REFERENCES transactions(transaction_id) ON DELETE RESTRICT`: Prevents removing the target transaction.
- `FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE RESTRICT`: Prevents deleting a customer record when financial history exists.

---

## 3. Reversal Execution Workflow (Milestone 2 Specification)

When an authorized reversal is initiated for transaction $T_{\text{orig}}$:

```text
    Original Transaction (T1)                      Compensating Entry (T2)
    +------------------------------+               +------------------------------+
    | transaction_id: 101          |               | transaction_id: 102          |
    | customer_id: 1               |               | customer_id: 1               |
    | type: CREDIT                 | <----------+  | type: PAYMENT (Compensating) |
    | amount_paise: 34000 (Rs 340) |            |  | amount_paise: 34000 (Rs 340) |
    | status: REVERSED             |            +--| reversed_transaction_id: 101 |
    +------------------------------+               | status: COMMITTED            |
                                                   +------------------------------+
```

### Steps inside atomic write transaction (`BEGIN IMMEDIATE`):
1. **Locate and Lock:** Fetch $T_{\text{orig}}$ using `BEGIN IMMEDIATE`. Verify it exists and is currently in `COMMITTED` status.
2. **Check Existing Reversal:** If `reversed_transaction_id = T_orig.id` already exists, return the existing reversal record (idempotent replay).
3. **Determine Compensating Direction:**
   - If $T_{\text{orig}}.\text{type} == \text{CREDIT}$ (customer debt increased by ₹340), the compensating entry $T_{\text{comp}}.\text{type} = \text{PAYMENT}$ with ₹340.
   - If $T_{\text{orig}}.\text{type} == \text{PAYMENT}$ (customer debt decreased by ₹200), the compensating entry $T_{\text{comp}}.\text{type} = \text{CREDIT}$ with ₹200.
4. **Append Compensating Transaction:**
   - Insert $T_{\text{comp}}$ with `reversed_transaction_id = T_orig.transaction_id`, status `COMMITTED`.
5. **Update Original Status:**
   - Update $T_{\text{orig}}.\text{status} = \text{'REVERSED'}$.
6. **Commit:** Both the status update and the new compensating entry commit together.

---

## 4. Balance Calculation Invariants

In Vaada Khata, the outstanding customer balance represents the amount the customer owes the merchant.

### Model A: Status-Filtered Active Calculation
$$\text{Outstanding Balance} = \sum_{\substack{t \in \text{transactions} \\ t.\text{status} = \text{'COMMITTED'}}} \text{CREDIT} - \sum_{\substack{t \in \text{transactions} \\ t.\text{status} = \text{'COMMITTED'}}} \text{PAYMENT}$$

### Model B: Full Additive Compensating Calculation
Because $T_{\text{comp}}$ directly offsets $T_{\text{orig}}$:
$$\text{Net Effect of } (T_{\text{orig}} + T_{\text{comp}}) = +34000\text{ paise} - 34000\text{ paise} = 0$$

Both models produce identical results. In our engine, status filtering allows clear reporting in customer statements (e.g. marking the entry clearly as *Reversed / Compensated*).

---

## 5. Promises are Isolated from Balances

Payment promises stored in the `promises` table:
- **Never alter the balance sum:**
  $$\text{Balance} = f(\text{committed transactions only})$$
- Promises remain in `PENDING` until fulfilled by an actual committed payment transaction.
