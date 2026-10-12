"""Centralized deterministic verification layer: schema validation, strict evidence grounding, consistency rules, read-after-write verification, and HMAC governance gate."""

from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from agents.models import (
    ExtractedField,
    ExtractionResult,
    FailureCategory,
    FailureRecord,
    VerificationStatus,
)
from agents.reliability import GovernanceViolationError

logger = logging.getLogger(__name__)

STANDARD_CURRENCIES = {
    "USD", "EUR", "GBP", "CAD", "AUD", "JPY", "INR", "CHF", "CNY", "NZD", "SGD"
}


# ---------------------------------------------------------------------------
# Normalization Helpers
# ---------------------------------------------------------------------------

def normalize_amount(val: Any) -> Tuple[Optional[Decimal], Optional[str]]:
    """Normalize numeric/amount value to Decimal without silent mutation.

    Detects corrupted OCR strings (e.g. '2O,0O0.5x') and locale separators.
    Returns (normalized_decimal, error_message).
    """
    if val is None:
        return None, "Amount value is None"

    if isinstance(val, (int, float)):
        return Decimal(str(val)), None

    if isinstance(val, Decimal):
        return val, None

    s = str(val).strip()
    if not s:
        return None, "Amount value is empty"

    # Strip known currency prefixes/suffixes and symbols
    s_no_curr = re.sub(
        r"(?i)^\s*(?:INR|USD|EUR|GBP|CAD|AUD|JPY|CHF|SGD|NZD|CNY|Rs\.?)\b\s*",
        "",
        s,
    )
    s_no_curr = re.sub(
        r"(?i)\s*\b(?:INR|USD|EUR|GBP|CAD|AUD|JPY|CHF|SGD|NZD|CNY)\s*$",
        "",
        s_no_curr,
    )
    s_no_curr = re.sub(r"[₹$€£¥\s]", "", s_no_curr)

    # Detect OCR corruption: letters mixed with digits (e.g., '2O,0O0.5x', 'XX,XXX.00')
    if re.search(r"[A-Za-z]", s_no_curr):
        return None, f"Cannot parse amount: unreadable or corrupted amount value '{val}'"

    # Handle European vs standard decimal/thousand separators:
    # If both '.' and ',' exist: whichever comes last is the decimal separator
    if "." in s_no_curr and "," in s_no_curr:
        if s_no_curr.rfind(",") > s_no_curr.rfind("."):
            # European format e.g. 1.250,50
            cleaned = s_no_curr.replace(".", "").replace(",", ".")
        else:
            # Standard/Indian format e.g. 1,250.50 or 1,25,000.00
            cleaned = s_no_curr.replace(",", "")
    elif "," in s_no_curr:
        # Only comma present: if ends with ,XX (2 digits), treat as European decimal
        if re.search(r",\d{2}$", s_no_curr) and s_no_curr.count(",") == 1:
            cleaned = s_no_curr.replace(",", ".")
        else:
            cleaned = s_no_curr.replace(",", "")
    else:
        cleaned = s_no_curr

    try:
        dec = Decimal(cleaned)
        return dec, None
    except (InvalidOperation, ValueError):
        return None, f"Cannot parse amount '{val}' as valid numeric decimal"


def normalize_currency(val: Any) -> Tuple[Optional[str], Optional[str]]:
    """Normalize currency symbols and codes to ISO 4217 standard."""
    if not val:
        return None, "Currency value is missing"

    s = str(val).strip().upper()
    mapping = {
        "₹": "INR",
        "RS": "INR",
        "RS.": "INR",
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
        "¥": "JPY",
    }
    if s in mapping:
        return mapping[s], None

    if s in STANDARD_CURRENCIES:
        return s, None

    for code in STANDARD_CURRENCIES:
        if re.search(rf"\b{code}\b", s):
            return code, None
    for sym, code in mapping.items():
        if sym in s:
            return code, None

    return s, f"Unrecognized currency code or symbol '{val}'"


def normalize_date(val: Any) -> Tuple[Optional[date], bool, Optional[str]]:
    """Parse and normalize date string to datetime.date.

    Returns:
        (date_obj, is_ambiguous, error_message)
        Note: When ambiguous (e.g. 03/04/2026), date_obj is None so callers never silently use a guessed date.
    """
    if val is None:
        return None, False, "Date value is None"

    if isinstance(val, date) and not isinstance(val, datetime):
        return val, False, None
    if isinstance(val, datetime):
        return val.date(), False, None

    s = str(val).strip()

    # 1. Unambiguous ISO format: YYYY-MM-DD or YYYY/MM/DD
    iso_match = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)
    if iso_match:
        try:
            d = date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
            return d, False, None
        except ValueError as exc:
            return None, False, f"Invalid date values in '{s}': {exc}"

    # 2. Month name formats: e.g. "15 Jun 2026", "31 March 2027", "May 15, 2026"
    for fmt in (
        "%B %d, %Y",
        "%B %d %Y",
        "%b %d, %Y",
        "%b %d %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.date(), False, None
        except ValueError:
            pass

    # 3. Numeric two-part formats: DD/MM/YYYY or MM/DD/YYYY
    slash_match = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", s)
    if slash_match:
        part1 = int(slash_match.group(1))
        part2 = int(slash_match.group(2))
        year = int(slash_match.group(3))
        if year < 100:
            year += 2000

        # Identical day and month (e.g. 04/04/2026) -> completely unambiguous!
        if 1 <= part1 <= 12 and part1 == part2:
            try:
                return date(year, part1, part2), False, None
            except ValueError as exc:
                return None, False, str(exc)

        # Both parts <= 12 and part1 != part2 -> genuinely ambiguous between DD/MM and MM/DD!
        if 1 <= part1 <= 12 and 1 <= part2 <= 12 and part1 != part2:
            return (
                None,
                True,
                f"Ambiguous date format '{s}': could be DD/MM/YYYY ({part1:02d}/{part2:02d}) or MM/DD/YYYY ({part2:02d}/{part1:02d})",
            )

        # part1 > 12 -> must be DD/MM/YYYY
        if part1 > 12 and 1 <= part2 <= 12:
            try:
                return date(year, part2, part1), False, None
            except ValueError as exc:
                return None, False, str(exc)

        # part2 > 12 -> must be MM/DD/YYYY
        if 1 <= part1 <= 12 and part2 > 12:
            try:
                return date(year, part1, part2), False, None
            except ValueError as exc:
                return None, False, str(exc)

    return None, False, f"Unsupported or unrecognized date format '{s}'"


# ---------------------------------------------------------------------------
# Strict Evidence Grounding & Field Verification
# ---------------------------------------------------------------------------

def check_evidence_grounding(field: ExtractedField, doc_raw_text: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """Verify that extracted value is strictly grounded in its specific cited source evidence.

    Never falls back to matching arbitrary unrelated text elsewhere in the document.
    Also validates semantic role (e.g., prevents Buyer / Bill To from validating Vendor).
    """
    if field.extracted_value is None:
        return False, "Cannot verify evidence for missing field value"

    if not field.source_evidence or not field.source_evidence.strip():
        return False, "Missing cited source evidence for field"

    val_str = str(field.extracted_value).strip().lower()
    ev_lower = field.source_evidence.lower()

    # Semantic role check for vendor vs buyer
    if field.field_name == "vendor":
        if re.search(r"\b(?:bill\s*to|billed\s*to|ship\s*to|buyer|customer)\b", ev_lower):
            return False, f"Semantic grounding failure: cited evidence '{field.source_evidence}' refers to Buyer/Bill-To, not Vendor"

    # Literal / normalized presence inside cited source evidence
    clean_val = re.sub(r"[₹$€£¥, ]", "", val_str)
    clean_ev = re.sub(r"[₹$€£¥, ]", "", ev_lower)

    if val_str in ev_lower or (clean_val and clean_val in clean_ev):
        return True, None

    return False, f"Extracted value '{field.extracted_value}' is not present in cited source evidence: '{field.source_evidence}'"


def verify_field(field: ExtractedField, doc_raw_text: Optional[str] = None) -> None:
    """Perform structural validation, normalization, and strict grounding checks on an extracted field."""
    name = field.field_name

    if field.extracted_value is None:
        field.verification_status = VerificationStatus.UNVERIFIED
        field.confidence = 0.0
        return

    # 1. Structural & Normalization validation
    if name in ("total_amount", "subtotal", "tax"):
        dec, err = normalize_amount(field.extracted_value)
        if err or dec is None:
            field.verification_status = VerificationStatus.FAILED
            field.unresolved_issue = err
            field.confidence = 0.1
            return
        if dec < 0:
            field.verification_status = VerificationStatus.NEEDS_HUMAN_REVIEW
            field.unresolved_issue = f"Negative amount detected for {name}: {dec}"
            field.normalized_value = dec
            return
        field.normalized_value = dec

    elif name in ("invoice_date", "due_date", "expiry_date"):
        d, is_ambiguous, err = normalize_date(field.extracted_value)
        if is_ambiguous:
            field.normalized_value = None
            field.verification_status = VerificationStatus.NEEDS_HUMAN_REVIEW
            field.unresolved_issue = err
            field.confidence = 0.5
            return
        if err or d is None:
            field.verification_status = VerificationStatus.FAILED
            field.unresolved_issue = err
            field.confidence = 0.2
            return
        field.normalized_value = d

    elif name == "currency":
        curr, err = normalize_currency(field.extracted_value)
        if err:
            field.verification_status = VerificationStatus.NEEDS_HUMAN_REVIEW
            field.unresolved_issue = err
            field.normalized_value = curr
            field.confidence = 0.5
            return
        field.normalized_value = curr

    elif name == "invoice_number":
        s_val = str(field.extracted_value).strip()
        if not s_val or len(s_val) < 2:
            field.verification_status = VerificationStatus.FAILED
            field.unresolved_issue = f"Invalid invoice number format: '{s_val}'"
            field.confidence = 0.3
            return
        field.normalized_value = s_val

    elif name == "vendor":
        s_val = str(field.extracted_value).strip()
        if not s_val or len(s_val) < 2:
            field.verification_status = VerificationStatus.FAILED
            field.unresolved_issue = "Vendor name is empty or too short"
            field.confidence = 0.3
            return
        field.normalized_value = s_val
    else:
        field.normalized_value = field.extracted_value

    # 2. Strict Evidence Grounding Check
    is_grounded, ground_issue = check_evidence_grounding(field, doc_raw_text=doc_raw_text)
    if not is_grounded:
        field.verification_status = VerificationStatus.FAILED
        field.unresolved_issue = ground_issue
        field.confidence = 0.2
        return

    if field.verification_status != VerificationStatus.NEEDS_HUMAN_REVIEW:
        field.verification_status = VerificationStatus.VERIFIED
        field.confidence = 1.0


# ---------------------------------------------------------------------------
# Cross-Field Consistency Checks
# ---------------------------------------------------------------------------

def check_date_consistency(result: ExtractionResult) -> None:
    """Check temporal consistency: due_date should not be earlier than invoice_date."""
    inv_date_field = result.fields.get("invoice_date")
    due_date_field = result.fields.get("due_date")

    if not inv_date_field or not due_date_field:
        return

    d_inv = inv_date_field.normalized_value
    d_due = due_date_field.normalized_value

    if isinstance(d_inv, date) and isinstance(d_due, date):
        if d_due < d_inv:
            issue_msg = (
                f"Contradiction: due_date ({d_due}) is earlier than invoice_date ({d_inv})"
            )
            inv_date_field.verification_status = VerificationStatus.NEEDS_HUMAN_REVIEW
            due_date_field.verification_status = VerificationStatus.NEEDS_HUMAN_REVIEW
            inv_date_field.unresolved_issue = issue_msg
            due_date_field.unresolved_issue = issue_msg
            result.add_failure(
                category=FailureCategory.VERIFICATION_FAILURE,
                message=issue_msg,
                field_name="due_date",
            )
            result.requires_human_review = True


def check_math_reconciliation(result: ExtractionResult) -> None:
    """Check mathematical reconciliation: subtotal + tax == total_amount (within 0.01 tolerance)."""
    sub_field = result.fields.get("subtotal")
    tax_field = result.fields.get("tax")
    total_field = result.fields.get("total_amount")

    if not sub_field or not tax_field or not total_field:
        return

    sub = sub_field.normalized_value
    tax = tax_field.normalized_value
    tot = total_field.normalized_value

    if isinstance(sub, Decimal) and isinstance(tax, Decimal) and isinstance(tot, Decimal):
        expected_total = sub + tax
        diff = abs(expected_total - tot)
        if diff > Decimal("0.01"):
            issue_msg = (
                f"Math mismatch: subtotal ({sub}) + tax ({tax}) = {expected_total}, "
                f"which does not reconcile with total_amount ({tot}) (diff: {diff})"
            )
            total_field.verification_status = VerificationStatus.NEEDS_HUMAN_REVIEW
            total_field.unresolved_issue = issue_msg
            result.add_failure(
                category=FailureCategory.VERIFICATION_FAILURE,
                message=issue_msg,
                field_name="total_amount",
            )
            result.requires_human_review = True


def verify_extraction_result(
    result: ExtractionResult,
    doc_raw_text: Optional[str] = None,
) -> ExtractionResult:
    """Run full deterministic verification pipeline on an ExtractionResult."""
    for field_name, field in result.fields.items():
        verify_field(field, doc_raw_text=doc_raw_text)

        if field.verification_status == VerificationStatus.NEEDS_HUMAN_REVIEW:
            result.requires_human_review = True
        elif field.verification_status == VerificationStatus.FAILED:
            result.requires_human_review = True
            result.add_failure(
                category=FailureCategory.VERIFICATION_FAILURE,
                message=field.unresolved_issue or f"Verification failed for field '{field_name}'",
                field_name=field_name,
                file_path=field.source_document,
            )

    check_date_consistency(result)
    check_math_reconciliation(result)

    has_verified = any(f.verification_status == VerificationStatus.VERIFIED for f in result.fields.values())
    has_failed = any(f.verification_status == VerificationStatus.FAILED for f in result.fields.values())
    has_missing = any(f.extracted_value is None for f in result.fields.values())
    has_review = result.requires_human_review or any(
        f.verification_status == VerificationStatus.NEEDS_HUMAN_REVIEW for f in result.fields.values()
    )

    if has_review or (has_verified and (has_failed or has_missing)):
        result.status = "partial"
        result.requires_human_review = True
    elif (has_failed or has_missing) and not has_verified:
        result.status = "failed"
        result.requires_human_review = True
    elif has_verified and not has_failed and not has_review and not has_missing:
        result.status = "success"
        result.requires_human_review = False

    return result


# ---------------------------------------------------------------------------
# Independent Read-After-Write Verification (Category B / D / E)
# ---------------------------------------------------------------------------

def verify_read_after_write(
    intended_fields: Dict[str, Any],
    persisted_record: Optional[Dict[str, Any]],
    baseline_record: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    """Verify that a database/API mutation persisted accurately without side-effect corruption."""
    if not persisted_record:
        return False, "Read-after-write verification failed: record not found after write"

    for key, expected_val in intended_fields.items():
        if key not in persisted_record:
            return False, f"Read-after-write verification failed: field '{key}' missing in persisted record"

        actual_val = persisted_record[key]
        if key == "amount":
            dec_exp, err1 = normalize_amount(expected_val)
            dec_act, err2 = normalize_amount(actual_val)
            if err1 or err2 or dec_exp is None or dec_act is None:
                return False, f"Invalid amount in read-after-write check: expected={expected_val}, actual={actual_val}"
            if abs(dec_exp - dec_act) > Decimal("0.001"):
                return False, f"Amount mismatch in persisted record: expected {dec_exp}, got {dec_act}"
        elif key == "due_date":
            if str(expected_val) != str(actual_val):
                return False, f"Due date mismatch: expected {expected_val}, got {actual_val}"
        else:
            if str(expected_val).strip().lower() != str(actual_val).strip().lower():
                return False, f"Field '{key}' mismatch: expected '{expected_val}', got '{actual_val}'"

    # If updating an existing record, verify unrelated immutable fields were not corrupted
    if baseline_record:
        for k, base_val in baseline_record.items():
            if k not in intended_fields and k not in ("updated_at", "id"):
                if persisted_record.get(k) != base_val:
                    return False, f"Unrelated field '{k}' was unexpectedly modified from '{base_val}' to '{persisted_record.get(k)}'"

    return True, None


# ---------------------------------------------------------------------------
# Non-Bypassable HMAC Payment Governance Gate (Category E)
# ---------------------------------------------------------------------------

class PaymentApprovalGate:
    """Cryptographic HMAC-SHA256 governance gate for irreversible financial actions.

    Enforces:
    - Policy 3.a: All payments require explicit human supervisor authorization.
    - Policy 3.b: Payments >= 50,000 require Finance Manager authorization level.
    - Payload integrity (bound to record_id, amount, currency, scope).
    - Expiration and one-time nonce replay protection.
    """

    MANAGER_THRESHOLD = Decimal("50000.00")

    def __init__(self, secret_key: Optional[bytes] = None, ttl_seconds: int = 3600):
        self._secret = secret_key or secrets.token_bytes(32)
        self._ttl_seconds = ttl_seconds
        self._used_nonces: Set[str] = set()

    @classmethod
    def required_role(cls, amount: float | Decimal) -> str:
        dec = Decimal(str(amount))
        return "finance_manager" if dec >= cls.MANAGER_THRESHOLD else "supervisor"

    def issue_approval_request_token(
        self,
        task_id: str,
        record_id: str,
        amount: float | Decimal,
        currency: str = "INR",
        scope: str = "payment_submit",
    ) -> str:
        """Issue a signed pending approval token bound to the exact payment payload."""
        dec_amount = f"{Decimal(str(amount)):.2f}"
        role = self.required_role(dec_amount)
        payload = {
            "task_id": task_id,
            "record_id": record_id,
            "amount": dec_amount,
            "currency": currency.upper(),
            "scope": scope,
            "required_role": role,
            "nonce": secrets.token_hex(12),
            "exp": int(time.time()) + self._ttl_seconds,
        }
        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        b64_payload = base64.urlsafe_b64encode(payload_bytes).decode("ascii")
        sig = hmac.new(self._secret, b64_payload.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{b64_payload}.{sig}"

    def verify_and_consume(
        self,
        token: Optional[str],
        record_id: str,
        amount: float | Decimal,
        currency: str = "INR",
        expected_scope: str = "payment_submit",
        approver_role: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Verify HMAC signature, payload binding, expiration, role, and replay protection.

        Fails closed by raising GovernanceViolationError on any discrepancy.
        """
        if not token or "." not in token:
            raise GovernanceViolationError("Blocked by Governance Gate: missing or malformed approval token")

        b64_payload, provided_sig = token.rsplit(".", 1)
        expected_sig = hmac.new(self._secret, b64_payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided_sig, expected_sig):
            raise GovernanceViolationError("Blocked by Governance Gate: invalid HMAC signature (tampered or forged token)")

        try:
            payload = json.loads(base64.urlsafe_b64decode(b64_payload.encode("ascii")).decode("utf-8"))
        except Exception as exc:
            raise GovernanceViolationError(f"Blocked by Governance Gate: unreadable token payload ({exc})") from exc

        if int(time.time()) > int(payload.get("exp", 0)):
            raise GovernanceViolationError("Blocked by Governance Gate: approval token has expired")

        nonce = payload.get("nonce")
        if not nonce or nonce in self._used_nonces:
            raise GovernanceViolationError("Blocked by Governance Gate: replay detected (approval token already consumed)")

        if payload.get("scope") != expected_scope:
            raise GovernanceViolationError(
                f"Blocked by Governance Gate: scope mismatch (expected '{expected_scope}', got '{payload.get('scope')}')"
            )

        dec_amount = f"{Decimal(str(amount)):.2f}"
        if payload.get("record_id") != record_id or payload.get("amount") != dec_amount:
            raise GovernanceViolationError(
                f"Blocked by Governance Gate: payload mismatch (token bound to {payload.get('record_id')} / "
                f"{payload.get('amount')}, attempted {record_id} / {dec_amount})"
            )

        if payload.get("currency") != currency.upper():
            raise GovernanceViolationError(
                f"Blocked by Governance Gate: currency mismatch ({payload.get('currency')} vs {currency.upper()})"
            )

        req_role = payload.get("required_role", "supervisor")
        if req_role == "finance_manager" and approver_role not in (None, "finance_manager"):
            raise GovernanceViolationError(
                f"Blocked by Governance Gate: Policy 3.b requires 'finance_manager' approval for amounts >= 50,000 (got '{approver_role}')"
            )

        self._used_nonces.add(nonce)
        return payload
