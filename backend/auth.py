"""
Core merchant authentication, session lifecycle, and brute-force protection.
Strictly enforces:
- Argon2id password/PIN hashing (never plain text).
- Constant-time dummy verification on nonexistent accounts to prevent timing attacks / enumeration.
- High-entropy cryptographic session tokens (secrets.token_urlsafe).
- SHA-256 token hashing in SQLite (raw tokens are never stored).
- Persistent rate limiting via login_attempts before expensive Argon2 checks.
- UTC-consistent session expiration and revocation.
- Strict derivation of merchant identity exclusively from verified session tokens.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import os
import re
import secrets
import sqlite3
from typing import Optional, Tuple

import argon2
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, Header, HTTPException, Request, status

from backend.auth_schemas import normalize_indian_phone, validate_pin
from backend.config import (
    AUTH_TOKEN_EXPIRY_HOURS,
    LEGACY_OWNER_PHONE,
    RATE_LIMIT_IP_MAX_ATTEMPTS,
    RATE_LIMIT_MAX_ATTEMPTS,
    RATE_LIMIT_PHONE_GLOBAL_MAX_ATTEMPTS,
    RATE_LIMIT_WINDOW_MINUTES,
    TRUSTED_PROXIES,
)
from backend.database import atomic, get_db
from backend.models import User


class AuthenticationError(Exception):
    """Raised when authentication credentials or session tokens are invalid."""
    pass


class RateLimitExceededError(Exception):
    """Raised when login attempt limits are exceeded."""
    pass


# ---------------------------------------------------------------------------
# Argon2id Configuration & Password Hasher
# Memory-hard protection against GPU/ASIC brute-force dictionary attacks.
# ---------------------------------------------------------------------------
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MB
    parallelism=4,
    hash_len=32,
    type=argon2.Type.ID,
)

# Pre-computed dummy hash for constant-time evaluation on nonexistent accounts
_DUMMY_ARGON2_HASH = _hasher.hash("999999")


def hash_credential(secret: str) -> str:
    """Hash secret using Argon2id."""
    return _hasher.hash(secret)


def verify_credential(secret: str, hashed: str) -> bool:
    """Verify secret against Argon2id hash."""
    try:
        return _hasher.verify(hashed, secret)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _constant_time_dummy_verify(secret: str) -> None:
    """Run dummy Argon2 verification to equalize timing when account does not exist."""
    try:
        _hasher.verify(_DUMMY_ARGON2_HASH, secret)
    except (VerifyMismatchError, InvalidHashError):
        pass


# ---------------------------------------------------------------------------
# Client IP Resolution
# ---------------------------------------------------------------------------
def get_client_ip(request: Request) -> Optional[str]:
    """
    Safely extract client IP address.
    Only respects X-Forwarded-For if direct peer is in configured TRUSTED_PROXIES.
    """
    if not request.client:
        return None
    direct_ip = request.client.host
    if direct_ip in TRUSTED_PROXIES:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # First IP in X-Forwarded-For is client origin
            return forwarded.split(",")[0].strip()
    return direct_ip


# ---------------------------------------------------------------------------
# Persistent Rate Limiting & Concurrency-Safe Attempt Reservation
# ---------------------------------------------------------------------------
def check_login_rate_limit(
    conn: sqlite3.Connection,
    phone: str,
    ip_address: Optional[str] = None,
) -> Tuple[bool, int, str]:
    """
    Check if login attempts for this phone number and/or IP have exceeded thresholds.
    Returns (is_allowed, retry_after_seconds, reason_message).
    Evaluates:
    1. Per-(Phone + IP) failed attempts since last success from this IP in rolling window.
       (Prevents an attacker on a different IP from locking out the merchant).
    2. Per-IP global flood limit across all accounts.
    3. Per-phone progressive throttling delay (if >= 10 failures across all IPs, enforce cooldown).
    4. Per-phone global circuit breaker across all IPs (if >= 20 distributed failures).
    """
    now_utc = datetime.now(timezone.utc)
    window_start = (now_utc - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    eff_ip = ip_address.strip() if ip_address and ip_address.strip() else None

    cursor = conn.cursor()

    # 1. Per-(Phone + IP) failed attempts since last success from this IP
    if eff_ip:
        cursor.execute(
            """
            SELECT COUNT(*) as failed_count, MAX(attempted_at) as last_attempt
            FROM login_attempts
            WHERE phone = ? AND ip_address = ? AND success = 0 AND attempted_at >= ?
              AND attempted_at > (
                  SELECT COALESCE(MAX(attempted_at), '1970-01-01 00:00:00')
                  FROM login_attempts
                  WHERE phone = ? AND ip_address = ? AND success = 1 AND attempted_at >= ?
              )
            """,
            (phone, eff_ip, window_start, phone, eff_ip, window_start),
        )
        row = cursor.fetchone()
        phone_ip_failed = row["failed_count"] if row else 0
        if phone_ip_failed >= RATE_LIMIT_MAX_ATTEMPTS:
            last_attempt_str = row["last_attempt"]
            try:
                last_dt = datetime.strptime(last_attempt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                elapsed = (now_utc - last_dt).total_seconds()
                retry_after = max(1, int((RATE_LIMIT_WINDOW_MINUTES * 60) - elapsed))
            except (ValueError, TypeError):
                retry_after = RATE_LIMIT_WINDOW_MINUTES * 60
            return False, retry_after, "Too many failed login attempts from this device/IP for this account."

        # 2. Per-IP global flood limit (20 failed attempts across all accounts)
        cursor.execute(
            """
            SELECT COUNT(*) as ip_failed_count
            FROM login_attempts
            WHERE ip_address = ? AND success = 0 AND attempted_at >= ?
            """,
            (eff_ip, window_start),
        )
        ip_row = cursor.fetchone()
        if ip_row and ip_row["ip_failed_count"] >= RATE_LIMIT_IP_MAX_ATTEMPTS:
            return False, RATE_LIMIT_WINDOW_MINUTES * 60, "Too many failed login attempts from this IP address."

    # 3. Phone-level checks across all IPs
    cursor.execute(
        """
        SELECT COUNT(*) as total_failed, MAX(attempted_at) as last_attempt
        FROM login_attempts
        WHERE phone = ? AND success = 0 AND attempted_at >= ?
          AND attempted_at > (
              SELECT COALESCE(MAX(attempted_at), '1970-01-01 00:00:00')
              FROM login_attempts
              WHERE phone = ? AND success = 1 AND attempted_at >= ?
          )
        """,
        (phone, window_start, phone, window_start),
    )
    phone_row = cursor.fetchone()
    phone_total_failed = phone_row["total_failed"] if phone_row else 0

    if not eff_ip:
        # Fallback when client IP cannot be determined: enforce RATE_LIMIT_MAX_ATTEMPTS on phone
        if phone_total_failed >= RATE_LIMIT_MAX_ATTEMPTS:
            last_attempt_str = phone_row["last_attempt"]
            try:
                last_dt = datetime.strptime(last_attempt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                elapsed = (now_utc - last_dt).total_seconds()
                retry_after = max(1, int((RATE_LIMIT_WINDOW_MINUTES * 60) - elapsed))
            except (ValueError, TypeError):
                retry_after = RATE_LIMIT_WINDOW_MINUTES * 60
            return False, retry_after, "Too many failed login attempts for this account."

    # 3a. Global phone circuit breaker (e.g. 20 distributed botnet failures across all IPs)
    if phone_total_failed >= RATE_LIMIT_PHONE_GLOBAL_MAX_ATTEMPTS:
        last_attempt_str = phone_row["last_attempt"]
        try:
            last_dt = datetime.strptime(last_attempt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            elapsed = (now_utc - last_dt).total_seconds()
            retry_after = max(1, int((RATE_LIMIT_WINDOW_MINUTES * 60) - elapsed))
        except (ValueError, TypeError):
            retry_after = RATE_LIMIT_WINDOW_MINUTES * 60
        return False, retry_after, "Too many failed login attempts for this account across multiple devices."

    # 3b. Progressive throttling delay on phone across all IPs (when >= 10 distributed failures)
    if phone_total_failed >= 10 and phone_row["last_attempt"]:
        try:
            last_dt = datetime.strptime(phone_row["last_attempt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            elapsed = (now_utc - last_dt).total_seconds()
            delay_needed = 2.0
            if elapsed < delay_needed:
                retry_after = max(1, int(delay_needed - elapsed + 0.99))
                return False, retry_after, f"Login attempts are throttled. Please wait {retry_after} second(s) before trying again."
        except (ValueError, TypeError):
            pass

    return True, 0, ""


def reserve_login_attempt(
    conn: sqlite3.Connection,
    phone: str,
    ip_address: Optional[str] = None,
) -> int:
    """
    Atomically check rate limits and reserve an attempt slot in login_attempts under BEGIN IMMEDIATE.
    Prevents race conditions where concurrent requests all bypass the rate limit.
    Returns attempt_id if allowed.
    Raises RateLimitExceededError if rate limit is exceeded.
    """
    with atomic(conn):
        is_allowed, retry_after, reason = check_login_rate_limit(conn, phone, ip_address)
        if not is_allowed:
            prefix = reason if reason else "Too many failed login attempts."
            raise RateLimitExceededError(
                f"{prefix} Please try again after {retry_after} seconds."
            )

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO login_attempts (phone, ip_address, attempted_at, success)
            VALUES (?, ?, ?, 0)
            """,
            (phone, ip_address, now_utc),
        )
        return cursor.lastrowid


def record_login_attempt(
    conn: sqlite3.Connection,
    phone: str,
    ip_address: Optional[str],
    success: bool,
) -> None:
    """Record login attempt in persistent SQLite login_attempts table."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO login_attempts (phone, ip_address, attempted_at, success)
        VALUES (?, ?, ?, ?)
        """,
        (phone, ip_address, now_utc, 1 if success else 0),
    )


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------
def create_session(
    conn: sqlite3.Connection,
    user_id: int,
    auth_method: str = "PIN",
    expiry_hours: Optional[int] = None,
) -> Tuple[str, str]:
    """
    Generate a cryptographically secure session token and store its SHA-256 hash.
    Returns (raw_token, expires_at_utc_iso).
    Raw token is never stored in SQLite.
    """
    hours = expiry_hours or AUTH_TOKEN_EXPIRY_HOURS
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    session_id = f"sess_{secrets.token_hex(12)}"

    now_utc = datetime.now(timezone.utc)
    expires_dt = now_utc + timedelta(hours=hours)
    now_str = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    expires_str = expires_dt.strftime("%Y-%m-%d %H:%M:%S")

    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO sessions (session_id, user_id, token_hash, auth_method, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (session_id, user_id, token_hash, auth_method, expires_str, now_str),
    )
    return raw_token, expires_str


def verify_session(conn: sqlite3.Connection, raw_token: str) -> Optional[User]:
    """
    Verify bearer session token against SHA-256 hash in sessions table.
    Enforces expiry, revocation, and active account status.
    Returns authenticated User if valid, None otherwise.
    """
    if not raw_token or not isinstance(raw_token, str):
        return None

    clean_token = raw_token.strip()
    token_hash = hashlib.sha256(clean_token.encode("utf-8")).hexdigest()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.*, s.expires_at, s.revoked_at
        FROM sessions s
        JOIN users u ON u.user_id = s.user_id
        WHERE s.token_hash = ?
        """,
        (token_hash,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    # Check revocation
    if row["revoked_at"] is not None:
        return None

    # Check expiration (UTC)
    if row["expires_at"] <= now_str:
        return None

    # Check merchant status
    if row["status"] != "ACTIVE":
        return None

    return User.from_row(row)


def revoke_session(conn: sqlite3.Connection, raw_token: str) -> bool:
    """
    Revoke an active session matching the raw bearer token.
    Sets revoked_at timestamp in SQLite. Returns True if revoked, False if not found.
    """
    if not raw_token or not isinstance(raw_token, str):
        return False

    clean_token = raw_token.strip()
    token_hash = hashlib.sha256(clean_token.encode("utf-8")).hexdigest()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE sessions
        SET revoked_at = ?
        WHERE token_hash = ? AND revoked_at IS NULL
        """,
        (now_str, token_hash),
    )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Registration & Login Service Workflows
# ---------------------------------------------------------------------------
def register_merchant(
    conn: sqlite3.Connection,
    phone: str,
    name: str,
    shop_name: str,
    pin: str,
) -> Tuple[User, str, str]:
    """
    Register a new merchant account with Argon2id PIN hash.
    Protects legacy accounts: registration cannot claim legacy owner phone numbers.
    Returns (user, raw_token, expires_at).
    """
    norm_phone = normalize_indian_phone(phone)
    valid_pin = validate_pin(pin)
    clean_name = name.strip()
    clean_shop = shop_name.strip()

    if len(clean_name) < 2:
        raise ValueError("Merchant name must be at least 2 characters.")
    if len(clean_shop) < 2:
        raise ValueError("Shop name must be at least 2 characters.")

    with atomic(conn):
        cursor = conn.cursor()

        # Critical legacy protection: Cannot register as the legacy owner
        cursor.execute("SELECT user_id, phone FROM users WHERE user_id = 1;")
        legacy_row = cursor.fetchone()
        if legacy_row:
            legacy_phone = legacy_row["phone"]
            if norm_phone == legacy_phone or norm_phone == "LEGACY_OWNER_1":
                raise ValueError("This phone number is reserved and cannot be registered via public registration.")

        if LEGACY_OWNER_PHONE and norm_phone == LEGACY_OWNER_PHONE:
            raise ValueError("This phone number is reserved and cannot be registered via public registration.")

        # Check duplicate phone
        cursor.execute("SELECT user_id FROM users WHERE phone = ?;", (norm_phone,))
        if cursor.fetchone():
            raise ValueError("A merchant with this phone number already exists.")

        # Hash PIN with Argon2id
        hashed_pin = hash_credential(valid_pin)

        # Insert new merchant (user_id >= 2)
        cursor.execute(
            """
            INSERT INTO users (phone, name, shop_name, password_hash, status)
            VALUES (?, ?, ?, ?, 'ACTIVE')
            """,
            (norm_phone, clean_name, clean_shop, hashed_pin),
        )
        new_uid = cursor.lastrowid

        cursor.execute("SELECT * FROM users WHERE user_id = ?;", (new_uid,))
        user_row = cursor.fetchone()
        user = User.from_row(user_row)

        raw_token, expires_at = create_session(conn, user.user_id, auth_method="PIN")
        return user, raw_token, expires_at


def authenticate_merchant(
    conn: sqlite3.Connection,
    phone: str,
    pin: str,
    ip_address: Optional[str] = None,
) -> Tuple[User, str, str]:
    """
    Authenticate a merchant by normalized phone number and PIN.
    Applies atomic attempt reservation before expensive Argon2 checks.
    Uses timing-safe dummy evaluation to prevent account enumeration.
    Strictly returns uniform 'Invalid phone number or PIN.' for all credential errors.
    Returns (user, raw_token, expires_at).
    """
    try:
        norm_phone = normalize_indian_phone(phone)
    except ValueError:
        norm_phone = phone.strip()

    # 1. Atomic rate limiting check & attempt reservation BEFORE Argon2
    attempt_id = reserve_login_attempt(conn, norm_phone, ip_address)

    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE phone = ?;", (norm_phone,))
    user_row = cursor.fetchone()

    # 2. Account existence and status check (all fail with uniform error and dummy verify)
    if not user_row:
        _constant_time_dummy_verify(pin)
        raise AuthenticationError("Invalid phone number or PIN.")

    if user_row["status"] != "ACTIVE":
        _constant_time_dummy_verify(pin)
        raise AuthenticationError("Invalid phone number or PIN.")

    stored_hash = user_row["password_hash"]
    if not stored_hash:
        _constant_time_dummy_verify(pin)
        raise AuthenticationError("Invalid phone number or PIN.")

    # 3. Argon2id verification
    if not verify_credential(pin, stored_hash):
        raise AuthenticationError("Invalid phone number or PIN.")

    # 4. Successful login: update reserved attempt to success and create session atomically
    with atomic(conn):
        cursor = conn.cursor()
        cursor.execute("UPDATE login_attempts SET success = 1 WHERE attempt_id = ?;", (attempt_id,))
        user = User.from_row(user_row)
        raw_token, expires_at = create_session(conn, user.user_id, auth_method="PIN")
        return user, raw_token, expires_at


# ---------------------------------------------------------------------------
# FastAPI Authentication Dependency
# ---------------------------------------------------------------------------
def get_current_merchant(
    authorization: Optional[str] = Header(None, description="Bearer session token"),
    db: sqlite3.Connection = Depends(get_db),
) -> User:
    """
    FastAPI dependency that authenticates the caller via Bearer session token.
    Derives merchant user_id exclusively from verified session in SQLite.
    Never trusts client-supplied user_id.
    Sets db.current_user_id only after successful verification.
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
    merchant = verify_session(db, raw_token)
    if not merchant:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Bind verified user_id to connection for this request
    db.current_user_id = merchant.user_id
    return merchant
