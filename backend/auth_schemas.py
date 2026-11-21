"""
Pydantic schemas and input validation for merchant authentication.
Strictly enforces:
- Normalized 10-digit Indian mobile numbers (6-9 prefix).
- Strong 6-digit numeric PINs (trivial sequences rejected).
- Safe responses (never exposes password/PIN hashes or session token hashes).
"""

import re
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


TRIVIAL_PINS = {
    # All same digits
    "000000", "111111", "222222", "333333", "444444",
    "555555", "666666", "777777", "888888", "999999",
    # Forward sequences
    "123456", "234567", "345678", "456789", "012345",
    # Backward sequences
    "654321", "765432", "876543", "987654", "543210",
    # Repeated patterns
    "121212", "123123", "112233", "223344",
}


def normalize_indian_phone(phone_input: str) -> str:
    """
    Normalize and validate Indian phone numbers:
    - Strips whitespace, dashes, parentheses.
    - Handles +91, 91 (12 digits), and leading 0 (11 digits).
    - Requires 10 digits starting with 6, 7, 8, or 9.
    """
    if not phone_input or not isinstance(phone_input, str):
        raise ValueError("Phone number must be a non-empty string.")
    clean = re.sub(r"[\s\-\(\)\.]", "", phone_input.strip())
    if clean.startswith("+91"):
        clean = clean[3:]
    elif clean.startswith("91") and len(clean) == 12:
        clean = clean[2:]
    elif clean.startswith("0") and len(clean) == 11:
        clean = clean[1:]

    if not (len(clean) == 10 and clean.isdigit() and clean[0] in "6789"):
        raise ValueError(
            "Invalid Indian mobile number. Must be a valid 10-digit number starting with 6, 7, 8, or 9."
        )
    return clean


def validate_pin(pin_input: str) -> str:
    """
    Validate 6-digit merchant PIN:
    - Exactly 6 numeric digits.
    - Rejects common/trivial PINs (e.g. 123456, 000000, 111111).
    """
    if not pin_input or not isinstance(pin_input, str):
        raise ValueError("PIN must be a 6-digit string.")
    clean = pin_input.strip()
    if not (len(clean) == 6 and clean.isdigit()):
        raise ValueError("PIN must be exactly 6 numeric digits.")
    if clean in TRIVIAL_PINS:
        raise ValueError("The chosen PIN is too common or trivial. Please choose a stronger 6-digit PIN.")
    return clean


class MerchantRegisterRequest(BaseModel):
    phone: str = Field(..., description="Indian mobile number (e.g. '9876543210' or '+91 98765 43210')")
    name: str = Field(..., min_length=2, max_length=100, description="Merchant full name")
    shop_name: str = Field(..., min_length=2, max_length=100, description="Store or shop name")
    pin: str = Field(..., description="6-digit secret PIN")

    # Extra fields forbidden to prevent client tampering
    model_config = ConfigDict(extra="forbid")

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_indian_phone(v)

    @field_validator("pin")
    @classmethod
    def validate_pin_field(cls, v: str) -> str:
        return validate_pin(v)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        clean = v.strip()
        if len(clean) < 2:
            raise ValueError("Merchant name must be at least 2 characters.")
        return clean

    @field_validator("shop_name")
    @classmethod
    def validate_shop_name(cls, v: str) -> str:
        clean = v.strip()
        if len(clean) < 2:
            raise ValueError("Shop name must be at least 2 characters.")
        return clean


class MerchantProfileResponse(BaseModel):
    user_id: int
    phone: str
    name: str
    shop_name: str
    status: str
    voice_enrolled: bool = False
    created_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class MerchantRegisterResponse(BaseModel):
    user_id: int
    phone: str
    name: str
    shop_name: str
    status: str
    token: str
    token_type: str = "Bearer"
    expires_at: str


class MerchantLoginRequest(BaseModel):
    phone: str = Field(..., description="Indian mobile number")
    pin: str = Field(..., description="6-digit secret PIN")

    model_config = ConfigDict(extra="forbid")

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_indian_phone(v)


class MerchantLoginResponse(BaseModel):
    token: str
    token_type: str = "Bearer"
    expires_at: str
    user: MerchantProfileResponse


class LogoutResponse(BaseModel):
    message: str = "Logged out successfully."
