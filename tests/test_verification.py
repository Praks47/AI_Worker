"""Tests for deterministic verification, grounding checks, normalization, and consistency rules."""

from datetime import date
from decimal import Decimal
import pytest

from agents.models import (
    ExtractedField,
    ExtractionResult,
    FailureCategory,
    SourceLocation,
    VerificationStatus,
)
from agents.verification import (
    check_date_consistency,
    check_evidence_grounding,
    check_math_reconciliation,
    normalize_amount,
    normalize_currency,
    normalize_date,
    verify_extraction_result,
    verify_field,
)


def test_amount_normalization():
    dec, err = normalize_amount("$1,250.50")
    assert dec == Decimal("1250.50")
    assert err is None

    dec, err = normalize_amount(300)
    assert dec == Decimal("300")

    dec, err = normalize_amount("invalid_amount")
    assert dec is None
    assert "Cannot parse amount" in err


def test_currency_normalization():
    code, err = normalize_currency("$")
    assert code == "USD"
    assert err is None

    code, err = normalize_currency("EUR")
    assert code == "EUR"
    assert err is None

    code, err = normalize_currency("XYZ_UNKNOWN")
    assert "Unrecognized currency" in err


def test_date_normalization_unambiguous_and_ambiguous():
    # Unambiguous ISO
    d, is_amb, err = normalize_date("2026-05-15")
    assert d == date(2026, 5, 15)
    assert is_amb is False

    # Unambiguous month name
    d, is_amb, err = normalize_date("May 15, 2026")
    assert d == date(2026, 5, 15)
    assert is_amb is False

    # Ambiguous: 05/06/2026 (could be May 6 or June 5)
    d, is_amb, err = normalize_date("05/06/2026")
    assert is_amb is True
    assert "Ambiguous date format" in err


def test_evidence_grounding_success_and_conflict():
    # Grounded value
    field_ok = ExtractedField(
        field_name="total_amount",
        extracted_value="500.00",
        source_evidence="Total Due: $500.00",
    )
    grounded, issue = check_evidence_grounding(field_ok)
    assert grounded is True
    assert issue is None

    # Conflicting / fabricated value not in source evidence
    field_bad = ExtractedField(
        field_name="total_amount",
        extracted_value="999.00",
        source_evidence="Total Due: $500.00",
    )
    grounded, issue = check_evidence_grounding(field_bad)
    assert grounded is False
    assert "not present in cited source evidence" in issue


def test_verify_field_with_conflict():
    field = ExtractedField(
        field_name="invoice_number",
        extracted_value="FABRICATED-123",
        source_evidence="Invoice No: REAL-999",
    )
    verify_field(field)
    assert field.verification_status == VerificationStatus.FAILED
    assert "not present in cited source evidence" in field.unresolved_issue


def test_date_consistency_contradiction():
    result = ExtractionResult(
        fields={
            "invoice_date": ExtractedField(
                field_name="invoice_date",
                extracted_value="2026-06-15",
                normalized_value=date(2026, 6, 15),
                source_evidence="Date: 2026-06-15",
            ),
            "due_date": ExtractedField(
                field_name="due_date",
                extracted_value="2026-06-01",  # Due date EARLIER than invoice date!
                normalized_value=date(2026, 6, 1),
                source_evidence="Due: 2026-06-01",
            ),
        }
    )
    check_date_consistency(result)
    assert result.requires_human_review is True
    assert result.fields["due_date"].verification_status == VerificationStatus.NEEDS_HUMAN_REVIEW
    assert "earlier than invoice_date" in result.fields["due_date"].unresolved_issue
    assert any(f.category == FailureCategory.VERIFICATION_FAILURE for f in result.failures)


def test_math_reconciliation_success_and_failure():
    # Matching math: 1000 + 100 == 1100
    res_ok = ExtractionResult(
        fields={
            "subtotal": ExtractedField(field_name="subtotal", extracted_value="1000.00", normalized_value=Decimal("1000.00"), source_evidence="Subtotal: 1000.00"),
            "tax": ExtractedField(field_name="tax", extracted_value="100.00", normalized_value=Decimal("100.00"), source_evidence="Tax: 100.00"),
            "total_amount": ExtractedField(field_name="total_amount", extracted_value="1100.00", normalized_value=Decimal("1100.00"), source_evidence="Total: 1100.00"),
        }
    )
    check_math_reconciliation(res_ok)
    assert not any(f.category == FailureCategory.VERIFICATION_FAILURE for f in res_ok.failures)

    # Mismatch: 1000 + 100 != 1250
    res_bad = ExtractionResult(
        fields={
            "subtotal": ExtractedField(field_name="subtotal", extracted_value="1000.00", normalized_value=Decimal("1000.00"), source_evidence="Subtotal: 1000.00"),
            "tax": ExtractedField(field_name="tax", extracted_value="100.00", normalized_value=Decimal("100.00"), source_evidence="Tax: 100.00"),
            "total_amount": ExtractedField(field_name="total_amount", extracted_value="1250.00", normalized_value=Decimal("1250.00"), source_evidence="Total: 1250.00"),
        }
    )
    check_math_reconciliation(res_bad)
    assert res_bad.requires_human_review is True
    assert res_bad.fields["total_amount"].verification_status == VerificationStatus.NEEDS_HUMAN_REVIEW
    assert "Math mismatch" in res_bad.fields["total_amount"].unresolved_issue


def test_mixture_of_successful_and_failed_fields():
    result = ExtractionResult(
        fields={
            "vendor": ExtractedField(
                field_name="vendor",
                extracted_value="Acme Corp",
                source_evidence="Vendor: Acme Corp",
            ),
            "invoice_number": ExtractedField(
                field_name="invoice_number",
                extracted_value="ACM-001",
                source_evidence="Invoice: ACM-001",
            ),
            "total_amount": ExtractedField(
                field_name="total_amount",
                extracted_value="invalid_not_a_number",
                source_evidence="Total: invalid_not_a_number",
            ),
        }
    )
    verified_res = verify_extraction_result(result)
    # Valid fields verified
    assert verified_res.fields["vendor"].verification_status == VerificationStatus.VERIFIED
    assert verified_res.fields["invoice_number"].verification_status == VerificationStatus.VERIFIED
    # Failed field failed
    assert verified_res.fields["total_amount"].verification_status == VerificationStatus.FAILED
    assert verified_res.status == "partial"
    assert verified_res.requires_human_review is True
    assert any(f.category == FailureCategory.VERIFICATION_FAILURE and f.field_name == "total_amount" for f in verified_res.failures)


def test_identical_day_month_date_and_ambiguous_none():
    # Identical day and month (04/04/2026) is unambiguous
    d_same, is_amb_same, err_same = normalize_date("04/04/2026")
    assert d_same == date(2026, 4, 4)
    assert is_amb_same is False
    assert err_same is None

    # Ambiguous date (03/04/2026) must NOT guess a normalized date
    d_amb, is_amb, err_amb = normalize_date("03/04/2026")
    assert d_amb is None
    assert is_amb is True
    assert err_amb is not None


def test_buyer_vs_vendor_semantic_grounding_rejection():
    buyer_field = ExtractedField(
        field_name="vendor",
        extracted_value="Meridian Works Pvt Ltd",
        source_evidence="Bill To: Meridian Works Pvt Ltd",
    )
    verify_field(buyer_field)
    assert buyer_field.verification_status == VerificationStatus.FAILED
    assert "Buyer/Bill-To" in (buyer_field.unresolved_issue or "")


def test_read_after_write_verification():
    from agents.verification import verify_read_after_write

    ok, err = verify_read_after_write(
        intended_fields={"invoice_number": "WE-2026-0412", "amount": 74250.0, "due_date": "2026-10-25"},
        persisted_record={"record_id": "INV-1004", "invoice_number": "WE-2026-0412", "amount": 74250.0, "due_date": "2026-10-25"},
    )
    assert ok is True
    assert err is None

    ok_bad, err_bad = verify_read_after_write(
        intended_fields={"invoice_number": "WE-2026-0412", "amount": 74250.0},
        persisted_record={"record_id": "INV-1004", "invoice_number": "WE-2026-0412", "amount": 70000.0},
    )
    assert ok_bad is False
    assert "Amount mismatch" in (err_bad or "")


def test_payment_approval_gate_non_bypassable():
    from agents.reliability import GovernanceViolationError
    from agents.verification import PaymentApprovalGate

    gate = PaymentApprovalGate()

    # 1. Missing or forged token fails closed
    with pytest.raises(GovernanceViolationError, match="missing or malformed"):
        gate.verify_and_consume(None, record_id="INV-1002", amount=48200.00)

    with pytest.raises(GovernanceViolationError, match="invalid HMAC signature"):
        gate.verify_and_consume("eyJ0ZXN0IjoxfQ==.deadbeef", record_id="INV-1002", amount=48200.00)

    # 2. Valid token succeeds once, then blocks replay
    tok = gate.issue_approval_request_token("task-1", "INV-1002", 48200.00, "INR")
    payload = gate.verify_and_consume(tok, record_id="INV-1002", amount=48200.00, currency="INR")
    assert payload["record_id"] == "INV-1002"

    with pytest.raises(GovernanceViolationError, match="replay detected"):
        gate.verify_and_consume(tok, record_id="INV-1002", amount=48200.00, currency="INR")

    # 3. High-value payment (>= 50,000) requires finance_manager role
    tok_high = gate.issue_approval_request_token("task-2", "INV-1004", 74250.00, "INR")
    with pytest.raises(GovernanceViolationError, match="finance_manager"):
        gate.verify_and_consume(
            tok_high,
            record_id="INV-1004",
            amount=74250.00,
            currency="INR",
            approver_role="supervisor",
        )
    payload_high = gate.verify_and_consume(
        tok_high,
        record_id="INV-1004",
        amount=74250.00,
        currency="INR",
        approver_role="finance_manager",
    )
    assert payload_high["required_role"] == "finance_manager"

