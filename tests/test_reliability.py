"""Tests for bounded retries, exponential backoff, image preprocessing fallbacks, and failure classification."""

from pathlib import Path
from typing import Any
import pytest

from agents.models import FailureCategory
from agents.reliability import (
    OCRQualityError,
    PostCommitTimeoutError,
    ReliableOCRRunner,
    RetryPolicy,
    SimpleGrayscaleImage,
    TransientInfrastructureError,
    UnsupportedFormatError,
    classify_exception,
    classify_http_status,
    execute_with_retry,
    generate_idempotency_key,
)


class FlakyOCREngine:
    """Mock OCR engine that fails transiently N times then succeeds."""
    def __init__(self, fail_count: int, return_text: str = "Invoice #RETRY-100\nTotal: $800.00"):
        self.fail_count = fail_count
        self.calls = 0
        self.return_text = return_text

    def extract_text(self, image: Any) -> str:
        self.calls += 1
        if self.calls <= self.fail_count:
            raise TransientInfrastructureError(f"Transient OCR service unavailable (call {self.calls})")
        return self.return_text


class LowQualityOCREngine:
    """Mock OCR engine that produces poor text on raw image, but good text on preprocessed image."""
    def __init__(self, good_text: str = "Vendor: Cyberdyne\nInvoice #PRE-200\nTotal: $1,400.00"):
        self.calls = 0
        self.good_text = good_text

    def extract_text(self, image: Any) -> str:
        self.calls += 1
        if getattr(image, "mode", None) == "L":
            return self.good_text
        return "..."  # Unusable / low quality text on raw image


def test_ocr_failure_followed_by_successful_retry():
    flaky = FlakyOCREngine(fail_count=2, return_text="Acme Corp Invoice #OK-1\nTotal: $250.00")
    sleeps = []
    policy = RetryPolicy(
        max_retries=3,
        initial_backoff=0.01,
        backoff_factor=2.0,
        sleep_fn=lambda s: sleeps.append(s),
    )
    runner = ReliableOCRRunner(flaky, retry_policy=policy)

    dummy_img = SimpleGrayscaleImage(50, 50, b"\xff" * 2500)
    result_text = runner.extract_text(dummy_img)

    # Failed 2 times, succeeded on 3rd attempt
    assert flaky.calls == 3
    assert "Acme Corp" in result_text
    # Exponential backoff verified: 0.01, 0.02
    assert len(sleeps) == 2
    assert sleeps[0] == pytest.approx(0.01)
    assert sleeps[1] == pytest.approx(0.02)


def test_ocr_failure_exhausts_retry_budget():
    flaky = FlakyOCREngine(fail_count=10)  # Always fails
    sleeps = []
    policy = RetryPolicy(
        max_retries=3,
        initial_backoff=0.01,
        backoff_factor=2.0,
        sleep_fn=lambda s: sleeps.append(s),
    )
    runner = ReliableOCRRunner(flaky, retry_policy=policy)

    dummy_img = SimpleGrayscaleImage(50, 50, b"\xff" * 2500)
    with pytest.raises(TransientInfrastructureError, match="Transient OCR service unavailable"):
        runner.extract_text(dummy_img)

    # Exactly 1 initial + 3 retries = 4 attempts total
    assert flaky.calls == 4
    assert len(sleeps) == 3


def test_non_retryable_error_does_not_retry():
    calls = 0
    def bad_operation():
        nonlocal calls
        calls += 1
        raise ValueError("Invalid image buffer")

    policy = RetryPolicy(max_retries=3, initial_backoff=0.01)
    with pytest.raises(ValueError, match="Invalid image buffer"):
        execute_with_retry(bad_operation, policy=policy)

    # Should fail immediately without retrying
    assert calls == 1


def test_post_commit_504_never_blindly_retried():
    calls = 0
    def timeout_after_commit():
        nonlocal calls
        calls += 1
        raise classify_http_status(504, "Gateway timeout after commit")

    policy = RetryPolicy(max_retries=4, initial_backoff=0.01)
    with pytest.raises(PostCommitTimeoutError):
        execute_with_retry(timeout_after_commit, policy=policy)

    # Must halt immediately on 504 so caller reconciles state before retrying
    assert calls == 1


def test_idempotency_key_deterministic():
    k1 = generate_idempotency_key("create_invoice", "Wayne Enterprises", "WE-2026-0412", "74250.00")
    k2 = generate_idempotency_key("create_invoice", "wayne enterprises", "WE-2026-0412", "74250.00")
    k3 = generate_idempotency_key("create_invoice", "Wayne Enterprises", "WE-2026-0413", "74250.00")
    assert k1 == k2
    assert k1 != k3


def test_ocr_preprocessing_fallback_recovery():
    engine = LowQualityOCREngine(good_text="Wayne Enterprises\nInvoice #WY-999\nTotal: $900.00")
    runner = ReliableOCRRunner(engine)

    dummy_img = SimpleGrayscaleImage(50, 50, b"\xff" * 2500)
    text = runner.extract_text(dummy_img)

    assert "Wayne Enterprises" in text
    assert "WY-999" in text
    assert engine.calls >= 2


def test_ocr_preprocessing_exhaustion():
    class AlwaysPoorOCREngine:
        def __init__(self):
            self.calls = 0
        def extract_text(self, image: Any) -> str:
            self.calls += 1
            return ""

    poor_engine = AlwaysPoorOCREngine()
    runner = ReliableOCRRunner(poor_engine)

    dummy_img = SimpleGrayscaleImage(50, 50, b"\xff" * 2500)
    with pytest.raises(OCRQualityError, match="OCR produced insufficient quality after recovery attempts"):
        runner.extract_text(dummy_img)

    assert poor_engine.calls > 1


def test_failure_classification():
    assert classify_exception(UnsupportedFormatError("bad ext")) == FailureCategory.UNSUPPORTED_FORMAT
    assert classify_exception(ValueError("Unsupported format .exe")) == FailureCategory.UNSUPPORTED_FORMAT
    assert classify_exception(TransientInfrastructureError("timeout")) == FailureCategory.TRANSIENT_INFRASTRUCTURE_FAILURE
    assert classify_exception(OCRQualityError("blurry image")) == FailureCategory.OCR_OR_TEXT_QUALITY_FAILURE
    assert classify_exception(FileNotFoundError("missing file")) == FailureCategory.FILE_ACCESS_OR_PARSING_FAILURE

