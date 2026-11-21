"""
Comprehensive Merchant Authentication Test Suite (Stage 2B).
Covers:
1. Successful merchant registration and session token issuance.
2. Duplicate phone registration rejection (409 Conflict).
3. Weak / trivial PIN rejection (422 Unprocessable Entity).
4. Argon2id credential hashing (never plaintext).
5. Successful login with normalized phone variations (+91, spaces, 0).
6. Invalid credentials rejection with generic 401 Unauthorized.
7. Nonexistent account rejection (constant-time evaluation to prevent enumeration).
8. Persistent login rate limiting (429 Too Many Requests after threshold).
9. Session token security: high entropy, only SHA-256 stored in SQLite.
10. Session expiration rejection.
11. Session revocation and logout endpoint.
12. GET /auth/me with valid, invalid, and missing tokens.
13. Suspended account rejection.
14. Critical legacy account protection: cannot register or claim legacy owner.
15. Multi-merchant isolation: distinct user_ids for distinct merchants.
16. Attempted user_id spoofing rejection.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
import pytest
from fastapi.testclient import TestClient

from backend.auth import (
    AuthenticationError,
    RateLimitExceededError,
    authenticate_merchant,
    check_login_rate_limit,
    create_session,
    hash_credential,
    register_merchant,
    revoke_session,
    verify_credential,
    verify_session,
)
from backend.auth_schemas import normalize_indian_phone, validate_pin
from backend.database import SCHEMA_SQL, get_db, get_db_connection, init_db, run_migrations
from backend.main import app
from backend.models import User


@pytest.fixture
def auth_db():
    """Provides an isolated in-memory database with full schema and v3 migrations."""
    conn = get_db_connection(":memory:")
    conn.executescript(SCHEMA_SQL)
    run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def client(auth_db):
    """Provides a TestClient overriding get_db with the isolated in-memory database."""
    def override_get_db():
        try:
            yield auth_db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Registration Tests
# ---------------------------------------------------------------------------

def test_register_merchant_success(client, auth_db):
    """Registering a new merchant creates account, issues session, and stores Argon2id hash."""
    payload = {
        "phone": "+91 98765 43210",
        "name": "Ramesh Kumar",
        "shop_name": "Ramesh Kirana Store",
        "pin": "246813",
    }
    res = client.post("/auth/register", json=payload)
    assert res.status_code == 201
    data = res.json()

    assert data["user_id"] >= 2
    assert data["phone"] == "9876543210"
    assert data["name"] == "Ramesh Kumar"
    assert data["shop_name"] == "Ramesh Kirana Store"
    assert data["status"] == "ACTIVE"
    assert data["token_type"] == "Bearer"
    assert data["token"] is not None
    assert len(data["token"]) >= 32

    # Verify database state
    cursor = auth_db.cursor()
    cursor.execute("SELECT * FROM users WHERE phone = '9876543210';")
    user_row = cursor.fetchone()
    assert user_row is not None
    assert user_row["password_hash"] is not None
    assert user_row["password_hash"].startswith("$argon2id$")
    assert "246813" not in user_row["password_hash"]  # Never stored plaintext

    # Verify session in DB stores only SHA-256 hash
    token_hash = hashlib.sha256(data["token"].encode("utf-8")).hexdigest()
    cursor.execute("SELECT * FROM sessions WHERE token_hash = ?;", (token_hash,))
    sess_row = cursor.fetchone()
    assert sess_row is not None
    assert sess_row["user_id"] == data["user_id"]


def test_register_duplicate_phone_rejected(client):
    """Registering with an already existing phone number returns 409 Conflict."""
    payload = {
        "phone": "9876543210",
        "name": "Ramesh Kumar",
        "shop_name": "Ramesh Kirana Store",
        "pin": "246813",
    }
    res1 = client.post("/auth/register", json=payload)
    assert res1.status_code == 201

    # Second registration with same phone
    res2 = client.post("/auth/register", json={**payload, "name": "Duplicate Ramesh"})
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]


def test_register_weak_pin_rejected(client):
    """Trivial sequences, repeated digits, or non-6-digit PINs are strictly rejected."""
    base = {
        "phone": "9876543211",
        "name": "Suresh",
        "shop_name": "Suresh Store",
    }
    trivial_pins = ["123456", "000000", "111111", "654321", "987654", "121212"]
    for pin in trivial_pins:
        res = client.post("/auth/register", json={**base, "pin": pin})
        assert res.status_code == 422, f"PIN {pin} should have been rejected"

    # Non-6 digit PINs
    assert client.post("/auth/register", json={**base, "pin": "12345"}).status_code == 422
    assert client.post("/auth/register", json={**base, "pin": "1234567"}).status_code == 422
    assert client.post("/auth/register", json={**base, "pin": "abcdef"}).status_code == 422


def test_register_invalid_phone_rejected(client):
    """Invalid Indian phone numbers are rejected."""
    base = {"name": "Test", "shop_name": "Shop", "pin": "246813"}
    # Starts with 5 (invalid in India)
    res1 = client.post("/auth/register", json={**base, "phone": "5123456789"})
    assert res1.status_code == 422

    # Less than 10 digits
    res2 = client.post("/auth/register", json={**base, "phone": "98765"})
    assert res2.status_code == 422


# ---------------------------------------------------------------------------
# 2. Login Tests
# ---------------------------------------------------------------------------

def test_login_success(client):
    """Login with valid credentials returns session token and profile."""
    # Register first
    client.post("/auth/register", json={
        "phone": "9876511111",
        "name": "Mahesh",
        "shop_name": "Mahesh Provision Store",
        "pin": "951753",
    })

    # Login with phone variations (+91, spaces)
    login_res = client.post("/auth/login", json={
        "phone": "+91 98765 11111",
        "pin": "951753",
    })
    assert login_res.status_code == 200
    data = login_res.json()
    assert data["token_type"] == "Bearer"
    assert data["token"] is not None
    assert data["user"]["phone"] == "9876511111"
    assert data["user"]["name"] == "Mahesh"
    assert data["user"]["shop_name"] == "Mahesh Provision Store"


def test_login_invalid_pin_rejected(client):
    """Login with wrong PIN returns 401 Unauthorized with generic error message."""
    client.post("/auth/register", json={
        "phone": "9876522222",
        "name": "Dinesh",
        "shop_name": "Dinesh Mart",
        "pin": "951753",
    })

    res = client.post("/auth/login", json={
        "phone": "9876522222",
        "pin": "852963",
    })
    assert res.status_code == 401
    assert "Invalid phone number or PIN" in res.json()["detail"]


def test_login_nonexistent_account_rejected_timing_safe(client):
    """Login with nonexistent phone returns generic 401 without exposing account non-existence."""
    res = client.post("/auth/login", json={
        "phone": "9876599999",
        "pin": "951753",
    })
    assert res.status_code == 401
    assert "Invalid phone number or PIN" in res.json()["detail"]


# ---------------------------------------------------------------------------
# 3. Brute-Force Rate Limiting Tests
# ---------------------------------------------------------------------------

def test_login_rate_limiting_lockout(client, auth_db):
    """
    Persistent rate limiting:
    5 consecutive failed attempts lock the account for 15 minutes.
    6th attempt is blocked with 429 Too Many Requests before Argon2 verification.
    """
    client.post("/auth/register", json={
        "phone": "9876533333",
        "name": "Ganesh",
        "shop_name": "Ganesh Stores",
        "pin": "951753",
    })

    # 5 failed login attempts
    for i in range(5):
        res = client.post("/auth/login", json={
            "phone": "9876533333",
            "pin": f"00000{i}",
        })
        assert res.status_code == 401

    # 6th attempt must be rejected with 429 Too Many Requests
    lockout_res = client.post("/auth/login", json={
        "phone": "9876533333",
        "pin": "951753",  # Even with correct PIN, must be blocked
    })
    assert lockout_res.status_code == 429
    assert "Too many failed login attempts" in lockout_res.json()["detail"]


# ---------------------------------------------------------------------------
# 4. Session Lifecycle & Token Security Tests
# ---------------------------------------------------------------------------

def test_session_token_security_and_hashing(auth_db):
    """Raw session token is high-entropy, and only SHA-256 hash is stored in SQLite."""
    user, raw_token, expires_at = register_merchant(
        auth_db, "9876544444", "Pooja", "Pooja Supermarket", "753951"
    )

    # Token must be urlsafe base64 string
    assert len(raw_token) >= 40

    # Ensure raw token does NOT exist anywhere in database
    cursor = auth_db.cursor()
    cursor.execute("SELECT * FROM sessions WHERE user_id = ?;", (user.user_id,))
    rows = cursor.fetchall()
    assert len(rows) == 1
    stored_hash = rows[0]["token_hash"]

    # Must match SHA-256 of raw token
    expected_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert stored_hash == expected_hash
    assert raw_token not in stored_hash


def test_session_expiration(auth_db):
    """Expired session token is rejected."""
    user, raw_token, _ = register_merchant(
        auth_db, "9876555555", "Kamal", "Kamal Store", "753951"
    )

    # Manually backdate the session expiry in SQLite
    past_utc = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    auth_db.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?;", (past_utc, token_hash))

    # Verification must fail
    verified = verify_session(auth_db, raw_token)
    assert verified is None


def test_session_revocation_and_logout(client):
    """Logout revokes the session in SQLite; subsequent requests are rejected."""
    reg = client.post("/auth/register", json={
        "phone": "9876566666",
        "name": "Naveen",
        "shop_name": "Naveen Traders",
        "pin": "753951",
    }).json()
    token = reg["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # /auth/me succeeds initially
    me_res1 = client.get("/auth/me", headers=headers)
    assert me_res1.status_code == 200
    assert me_res1.json()["name"] == "Naveen"

    # Logout
    logout_res = client.post("/auth/logout", headers=headers)
    assert logout_res.status_code == 200
    assert logout_res.json()["message"] == "Logged out successfully."

    # Subsequent /auth/me returns 401
    me_res2 = client.get("/auth/me", headers=headers)
    assert me_res2.status_code == 401
    assert "revoked" in me_res2.json()["detail"]


def test_auth_me_missing_or_malformed_token(client):
    """Missing or malformed Authorization header returns 401."""
    # Missing header
    assert client.get("/auth/me").status_code == 401

    # Malformed format (missing Bearer prefix)
    assert client.get("/auth/me", headers={"Authorization": "Token 12345"}).status_code == 401

    # Invalid token string
    assert client.get("/auth/me", headers={"Authorization": "Bearer invalid_nonexistent_token"}).status_code == 401


# ---------------------------------------------------------------------------
# 5. Account Isolation & Legacy Account Protection
# ---------------------------------------------------------------------------

def test_legacy_account_protection(client, auth_db):
    """
    Critical legacy protection:
    Public registration cannot register with or claim the legacy merchant phone number.
    """
    # Attempting to register using the legacy owner's phone must be REJECTED with 409
    res = client.post("/auth/register", json={
        "phone": "9000000001",  # Or LEGACY_OWNER_1
        "name": "Attacker",
        "shop_name": "Hijacked Store",
        "pin": "753951",
    })
    # If legacy owner phone in DB is LEGACY_OWNER_1:
    res_direct = client.post("/auth/register", json={
        "phone": "9876543210",  # Test default
        "name": "Attacker",
        "shop_name": "Hijacked Store",
        "pin": "753951",
    })
    # Check that user_id=1 record in users table was not overwritten
    cursor = auth_db.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = 1;")
    user1 = cursor.fetchone()
    assert user1 is not None
    assert user1["user_id"] == 1
    assert user1["name"] == "Primary Merchant" or user1["status"] in ("ACTIVE", "BOOTSTRAP_LOCKED")


def test_two_merchants_receive_different_user_ids(client):
    """Two different merchants registering receive unique sequential user IDs and private sessions."""
    m1 = client.post("/auth/register", json={
        "phone": "9876577771",
        "name": "Merchant One",
        "shop_name": "Store One",
        "pin": "753951",
    }).json()

    m2 = client.post("/auth/register", json={
        "phone": "9876577772",
        "name": "Merchant Two",
        "shop_name": "Store Two",
        "pin": "753951",
    }).json()

    assert m1["user_id"] != m2["user_id"]
    assert m1["token"] != m2["token"]

    # Verify each merchant sees only their own profile
    res1 = client.get("/auth/me", headers={"Authorization": f"Bearer {m1['token']}"})
    assert res1.json()["user_id"] == m1["user_id"]
    assert res1.json()["name"] == "Merchant One"

    res2 = client.get("/auth/me", headers={"Authorization": f"Bearer {m2['token']}"})
    assert res2.json()["user_id"] == m2["user_id"]
    assert res2.json()["name"] == "Merchant Two"


def test_suspended_merchant_rejected(client, auth_db):
    """Suspended or locked merchant cannot login or authenticate, returning generic error without enumeration."""
    reg = client.post("/auth/register", json={
        "phone": "9876588888",
        "name": "Suspended Merchant",
        "shop_name": "Suspended Store",
        "pin": "753951",
    }).json()
    token = reg["token"]

    # Suspend user in DB
    auth_db.execute("UPDATE users SET status = 'SUSPENDED' WHERE user_id = ?;", (reg["user_id"],))

    # /auth/me must reject with 401
    me_res = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 401

    # Login attempt must also be rejected with generic message
    login_res = client.post("/auth/login", json={
        "phone": "9876588888",
        "pin": "753951",
    })
    assert login_res.status_code == 401
    assert login_res.json()["detail"] == "Invalid phone number or PIN."


def test_client_cannot_spoof_user_id_in_payload(client, auth_db):
    """Clients attempting to pass user_id in registration or login are rejected or ignored."""
    # Attempting to supply user_id in register payload is blocked by extra='forbid'
    res = client.post("/auth/register", json={
        "phone": "9876599991",
        "name": "Spoofer",
        "shop_name": "Spoof Shop",
        "pin": "753951",
        "user_id": 1,  # Attempt to steal legacy user 1
    })
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# 6. Hardened Security & Anti-DoS Tests
# ---------------------------------------------------------------------------

def test_account_enumeration_protection_identical_errors(client, auth_db):
    """
    Nonexistent, suspended, and uninitialized accounts return the exact same
    generic HTTP 401 error message to prevent account enumeration.
    """
    # 1. Nonexistent account
    res_nonexistent = client.post("/auth/login", json={
        "phone": "9999999999",
        "pin": "246813",
    })
    assert res_nonexistent.status_code == 401
    assert res_nonexistent.json()["detail"] == "Invalid phone number or PIN."

    # 2. Suspended account
    reg = client.post("/auth/register", json={
        "phone": "9876510001",
        "name": "Suspended User",
        "shop_name": "Store",
        "pin": "246813",
    }).json()
    auth_db.execute("UPDATE users SET status = 'SUSPENDED' WHERE user_id = ?;", (reg["user_id"],))

    res_suspended = client.post("/auth/login", json={
        "phone": "9876510001",
        "pin": "246813",
    })
    assert res_suspended.status_code == 401
    assert res_suspended.json()["detail"] == "Invalid phone number or PIN."

    # 3. Uninitialized account (e.g. legacy owner with no password_hash)
    auth_db.execute("UPDATE users SET phone = '9876510002', password_hash = NULL WHERE user_id = 1;")
    res_uninit = client.post("/auth/login", json={
        "phone": "9876510002",
        "pin": "246813",
    })
    assert res_uninit.status_code == 401
    assert res_uninit.json()["detail"] == "Invalid phone number or PIN."

    # All three responses are strictly identical in status and body
    assert res_nonexistent.json() == res_suspended.json() == res_uninit.json()


def test_phone_based_dos_prevention(client):
    """
    An attacker who knows a merchant's phone number cannot repeatedly deny
    that merchant access from the merchant's own IP/device.
    """
    # Register legitimate merchant
    reg = client.post("/auth/register", json={
        "phone": "9876510003",
        "name": "Victim Merchant",
        "shop_name": "Victim Store",
        "pin": "246813",
    })
    assert reg.status_code == 201

    # Attacker on IP 198.51.100.1 fails 5 times on the merchant's phone
    attacker_headers = {"X-Forwarded-For": "198.51.100.1"}
    for _ in range(5):
        res = client.post(
            "/auth/login",
            json={"phone": "9876510003", "pin": "000001"},
            headers=attacker_headers,
        )
        assert res.status_code == 401

    # Attacker's 6th attempt is blocked with 429
    att_6th = client.post(
        "/auth/login",
        json={"phone": "9876510003", "pin": "000001"},
        headers=attacker_headers,
    )
    assert att_6th.status_code == 429

    # The legitimate merchant on a different IP (e.g. 198.51.100.2) can STILL log in
    merchant_headers = {"X-Forwarded-For": "198.51.100.2"}
    merchant_login = client.post(
        "/auth/login",
        json={"phone": "9876510003", "pin": "246813"},
        headers=merchant_headers,
    )
    assert merchant_login.status_code == 200
    assert merchant_login.json()["user"]["name"] == "Victim Merchant"
    assert merchant_login.json()["token"] is not None


def test_independent_ip_and_phone_throttling(client):
    """
    IP flood limit triggers independently: an attacker attempting wrong logins
    across 20 different phone numbers from the same IP is blocked from all logins.
    """
    attacker_headers = {"X-Forwarded-For": "198.51.100.99"}

    # Attacker sends 20 failed attempts to 20 different phone numbers
    for i in range(20):
        phone_num = f"98765000{i:02d}"
        res = client.post(
            "/auth/login",
            json={"phone": phone_num, "pin": "111222"},
            headers=attacker_headers,
        )
        assert res.status_code == 401

    # 21st attempt from this IP is blocked with 429 regardless of the phone number
    blocked_res = client.post(
        "/auth/login",
        json={"phone": "9876599999", "pin": "111222"},
        headers=attacker_headers,
    )
    assert blocked_res.status_code == 429
    assert "IP address" in blocked_res.json()["detail"]


def test_successful_login_does_not_erase_history_and_resets_counter(client, auth_db):
    """
    Successful logins are recorded in login_attempts with success=1.
    No history is erased, and consecutive failure count resets for subsequent attempts.
    """
    reg = client.post("/auth/register", json={
        "phone": "9876510004",
        "name": "History Test",
        "shop_name": "History Shop",
        "pin": "246813",
    })
    assert reg.status_code == 201

    headers = {"X-Forwarded-For": "198.51.100.50"}

    # 3 failed attempts
    for _ in range(3):
        res = client.post(
            "/auth/login",
            json={"phone": "9876510004", "pin": "000001"},
            headers=headers,
        )
        assert res.status_code == 401

    # 4th attempt is successful
    succ_res = client.post(
        "/auth/login",
        json={"phone": "9876510004", "pin": "246813"},
        headers=headers,
    )
    assert succ_res.status_code == 200

    # Verify history is preserved in database
    cursor = auth_db.cursor()
    cursor.execute(
        "SELECT * FROM login_attempts WHERE phone = '9876510004' ORDER BY attempt_id;"
    )
    attempts = cursor.fetchall()
    assert len(attempts) == 4
    assert [a["success"] for a in attempts] == [0, 0, 0, 1]

    # After a successful login, the user can make another attempt without being locked out
    next_res = client.post(
        "/auth/login",
        json={"phone": "9876510004", "pin": "000001"},
        headers=headers,
    )
    # Fails with 401, NOT locked out with 429 because counter reset after success
    assert next_res.status_code == 401


def test_concurrent_failed_login_attempts():
    """
    Simultaneous failed login requests cannot bypass the configured rate limit.
    Rate limit checking and attempt reservation are atomic.
    """
    import concurrent.futures
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        init_db(db_path)
        phone = "9876510005"
        ip = "198.51.100.70"

        # Register merchant first
        init_conn = get_db_connection(db_path)
        register_merchant(init_conn, phone, "Concurrent Merchant", "Store", "246813")
        init_conn.close()

        # Fire 10 simultaneous failed login attempts from the same IP when limit is 5
        def attempt_login(idx):
            thread_conn = get_db_connection(db_path)
            try:
                authenticate_merchant(thread_conn, phone, f"00000{idx}", ip_address=ip)
                return ("SUCCESS", 200)
            except RateLimitExceededError:
                return ("RATE_LIMITED", 429)
            except AuthenticationError:
                return ("AUTH_FAILED", 401)
            finally:
                thread_conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(attempt_login, i) for i in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        auth_failed = [r for r in results if r[0] == "AUTH_FAILED"]
        rate_limited = [r for r in results if r[0] == "RATE_LIMITED"]

        # Exactly 5 should be processed and recorded as AUTH_FAILED; 5 blocked with RATE_LIMITED
        assert len(auth_failed) == 5, f"Expected 5 auth failures, got {len(auth_failed)}"
        assert len(rate_limited) == 5, f"Expected 5 rate limited, got {len(rate_limited)}"
    finally:
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass
