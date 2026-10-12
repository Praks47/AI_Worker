"""Centralized, reusable reliability layer: bounded retries, backoff, HTTP 503/504 classification, idempotency, and OCR fallbacks."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, TypeVar

from agents.models import (
    CommitUncertainty,
    FailureCategory,
    FailureRecord,
)

try:
    from PIL import Image, ImageEnhance, ImageFilter
    HAS_PIL = True
except ImportError:
    Image = Any  # type: ignore
    HAS_PIL = False

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Recoverable & Domain Failure Exceptions
# ---------------------------------------------------------------------------

class TransientInfrastructureError(Exception):
    """Raised on temporary service unavailability (e.g. HTTP 503 before commit, OCR worker glitch)."""
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code
        self.uncertainty = CommitUncertainty.DEFINITELY_NOT_COMMITTED


class PostCommitTimeoutError(Exception):
    """Raised when an operation times out after potentially committing (e.g. HTTP 504 Gateway Timeout)."""
    def __init__(self, message: str, status_code: int = 504):
        super().__init__(message)
        self.status_code = status_code
        self.uncertainty = CommitUncertainty.OUTCOME_UNKNOWN


class OCRQualityError(Exception):
    """Raised when OCR produces insufficient, unreadable, or poor-quality text."""
    pass


class DocumentParsingError(Exception):
    """Raised when a document structure is corrupt or cannot be decoded."""
    pass


class UnsupportedFormatError(Exception):
    """Raised when an unsupported file format is encountered."""
    pass


class GovernanceViolationError(PermissionError):
    """Raised when an irreversible action is attempted without a valid HMAC approval token."""
    pass


# ---------------------------------------------------------------------------
# Bounded Retry & Reliability Policy
# ---------------------------------------------------------------------------

@dataclass
class RetryPolicy:
    """Configurable bounded retry policy with exponential backoff."""
    max_retries: int = 3
    initial_backoff: float = 0.05
    base_delay_s: Optional[float] = None
    max_delay_s: float = 2.0
    backoff_factor: float = 2.0
    retryable_exceptions: Tuple[Type[Exception], ...] = (TransientInfrastructureError,)
    sleep_fn: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.base_delay_s is not None:
            self.initial_backoff = self.base_delay_s
        else:
            self.base_delay_s = self.initial_backoff


# Alias for compatibility across finance/orchestrator modules
ReliabilityPolicy = RetryPolicy


def generate_idempotency_key(operation: str, *parts: Any) -> str:
    """Generate a deterministic SHA-256 idempotency key for mutation safety."""
    normalized = "|".join(str(p).strip().lower() for p in (operation, *parts))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"idemp_{digest}"


def classify_http_status(status_code: int, message: str = "") -> Exception:
    """Classify HTTP error status codes into before-commit vs after-commit uncertainty."""
    if status_code in (502, 503, 429):
        return TransientInfrastructureError(
            f"HTTP {status_code} transient failure (definitely not committed): {message}",
            status_code=status_code,
        )
    if status_code in (504, 408):
        return PostCommitTimeoutError(
            f"HTTP {status_code} timeout (outcome unknown / possibly committed): {message}",
            status_code=status_code,
        )
    return RuntimeError(f"HTTP {status_code} permanent error: {message}")


def execute_with_retry(
    operation: Callable[[], T],
    policy: Optional[RetryPolicy] = None,
    operation_name: str = "operation",
    audit_log: Optional[List[str]] = None,
) -> T:
    """Execute an operation with bounded retries and exponential backoff.

    IMPORTANT: PostCommitTimeoutError (HTTP 504) is NEVER blindly retried here;
    it is raised immediately so the caller can reconcile state before retrying.
    """
    retry_policy = policy or RetryPolicy()
    attempts = 0

    while True:
        try:
            res = operation()
            if attempts > 0 and audit_log is not None:
                audit_log.append(f"{operation_name} succeeded on attempt {attempts + 1}")
            return res
        except PostCommitTimeoutError as exc:
            # Never blindly repeat an operation whose commit outcome is unknown
            msg = f"{operation_name} encountered post-commit timeout ({exc}); halting blind retry for reconciliation"
            logger.warning(msg)
            if audit_log is not None:
                audit_log.append(msg)
            raise
        except retry_policy.retryable_exceptions as exc:
            attempts += 1
            if attempts > retry_policy.max_retries:
                msg = f"Exhausted retry budget ({retry_policy.max_retries} retries) for {operation_name}: {exc}"
                logger.error(msg)
                if audit_log is not None:
                    audit_log.append(msg)
                raise exc

            backoff = min(
                retry_policy.max_delay_s,
                retry_policy.initial_backoff * (retry_policy.backoff_factor ** (attempts - 1)),
            )
            msg = (
                f"Attempt {attempts}/{retry_policy.max_retries} for {operation_name} "
                f"failed with transient error: {exc}. Backing off {backoff:.3f}s"
            )
            logger.warning(msg)
            if audit_log is not None:
                audit_log.append(msg)
            retry_policy.sleep_fn(backoff)
        except Exception:
            # Non-retryable exception: fail immediately without wasteful retries
            raise


# ---------------------------------------------------------------------------
# Image Wrapper & Preprocessing Fallbacks for OCR
# ---------------------------------------------------------------------------

class SimpleGrayscaleImage:
    """Lightweight grayscale image representation when PIL is unavailable."""
    def __init__(self, width: int, height: int, data: bytes = b"", mode: str = "RGB", step: str = "raw"):
        self.width = width
        self.height = height
        self.size = (width, height)
        self.data = data
        self.mode = mode
        self.step = step
        self.format = "PNG"

    def copy(self) -> "SimpleGrayscaleImage":
        return SimpleGrayscaleImage(self.width, self.height, self.data, self.mode, self.step)

    def convert(self, mode: str) -> "SimpleGrayscaleImage":
        return SimpleGrayscaleImage(self.width, self.height, self.data, mode, self.step)


def preprocess_enhance_contrast(image: Any) -> Any:
    """Convert to grayscale and enhance contrast."""
    if HAS_PIL and isinstance(image, Image.Image):
        gray = image.convert("L")
        enhancer = ImageEnhance.Contrast(gray)
        return enhancer.enhance(2.0)
    if hasattr(image, "convert"):
        img = image.convert("L")
        img.step = "enhance_contrast"
        return img
    return SimpleGrayscaleImage(100, 100, mode="L", step="enhance_contrast")


def preprocess_binarize(image: Any, threshold: int = 140) -> Any:
    """Convert to grayscale and apply thresholding / binarization."""
    if HAS_PIL and isinstance(image, Image.Image):
        gray = image.convert("L")
        return gray.point(lambda p: 255 if p > threshold else 0, mode="1").convert("L")
    if hasattr(image, "convert"):
        img = image.convert("L")
        img.step = "binarize"
        return img
    return SimpleGrayscaleImage(100, 100, mode="L", step="binarize")


def preprocess_sharpen(image: Any) -> Any:
    """Sharpen and denoise image."""
    if HAS_PIL and isinstance(image, Image.Image):
        gray = image.convert("L")
        blurred = gray.filter(ImageFilter.SMOOTH)
        return blurred.filter(ImageFilter.SHARPEN)
    if hasattr(image, "convert"):
        img = image.convert("L")
        img.step = "sharpen"
        return img
    return SimpleGrayscaleImage(100, 100, mode="L", step="sharpen")


PREPROCESSING_FALLBACKS: List[Tuple[str, Callable[[Any], Any]]] = [
    ("enhance_contrast", preprocess_enhance_contrast),
    ("binarize", preprocess_binarize),
    ("sharpen", preprocess_sharpen),
]


# ---------------------------------------------------------------------------
# Reliable OCR Runner
# ---------------------------------------------------------------------------

class ReliableOCRRunner:
    """Coordinates OCR execution with bounded retries and image preprocessing fallbacks."""

    def __init__(
        self,
        ocr_engine: Any,
        retry_policy: Optional[RetryPolicy] = None,
        min_quality_chars: int = 10,
    ):
        self.ocr_engine = ocr_engine
        self.retry_policy = retry_policy or RetryPolicy()
        self.min_quality_chars = min_quality_chars
        self.last_recovery_log: List[str] = []

    def is_quality_sufficient(self, text: str) -> bool:
        """Check if OCR text has sufficient length and alphanumeric content."""
        clean = text.strip()
        if len(clean) < self.min_quality_chars:
            return False
        alphanumeric_count = sum(1 for c in clean if c.isalnum())
        return alphanumeric_count >= self.min_quality_chars // 2

    def extract_text(self, image: Any) -> str:
        """Extract text from image using bounded retries and preprocessing fallbacks."""
        recovery_log: List[str] = []
        self.last_recovery_log = recovery_log

        def run_base() -> str:
            return self.ocr_engine.extract_text(image)

        text = execute_with_retry(
            run_base,
            policy=self.retry_policy,
            operation_name="Base OCR",
            audit_log=recovery_log,
        )

        if self.is_quality_sufficient(text):
            return text

        recovery_log.append("Base OCR produced insufficient text quality")

        for method_name, preprocessor in PREPROCESSING_FALLBACKS:
            try:
                processed_img = preprocessor(image)

                def run_fallback() -> str:
                    return self.ocr_engine.extract_text(processed_img)

                fallback_text = execute_with_retry(
                    run_fallback,
                    policy=self.retry_policy,
                    operation_name=f"OCR with {method_name}",
                    audit_log=recovery_log,
                )

                if self.is_quality_sufficient(fallback_text):
                    msg = f"OCR recovered using image preprocessing fallback '{method_name}'"
                    logger.info(msg)
                    recovery_log.append(msg)
                    return fallback_text
                else:
                    recovery_log.append(f"Fallback '{method_name}' produced insufficient quality")
            except Exception as exc:
                recovery_log.append(f"Fallback '{method_name}' failed: {exc}")

        if text.strip():
            return text

        raise OCRQualityError(
            f"OCR produced insufficient quality after recovery attempts: {'; '.join(recovery_log)}"
        )


# ---------------------------------------------------------------------------
# Failure Classifier
# ---------------------------------------------------------------------------

def classify_exception(exc: Exception) -> FailureCategory:
    """Classify an exception into one of the standard FailureCategories."""
    if isinstance(exc, UnsupportedFormatError):
        return FailureCategory.UNSUPPORTED_FORMAT
    elif isinstance(exc, TransientInfrastructureError):
        return FailureCategory.TRANSIENT_INFRASTRUCTURE_FAILURE
    elif isinstance(exc, PostCommitTimeoutError):
        return FailureCategory.POST_COMMIT_TIMEOUT
    elif isinstance(exc, OCRQualityError):
        return FailureCategory.OCR_OR_TEXT_QUALITY_FAILURE
    elif isinstance(exc, GovernanceViolationError):
        return FailureCategory.GOVERNANCE_BLOCKED
    elif isinstance(exc, FileNotFoundError):
        return FailureCategory.FILE_ACCESS_OR_PARSING_FAILURE
    elif isinstance(exc, (DocumentParsingError, ValueError)):
        msg = str(exc).lower()
        if "unsupported" in msg:
            return FailureCategory.UNSUPPORTED_FORMAT
        return FailureCategory.FILE_ACCESS_OR_PARSING_FAILURE
    else:
        return FailureCategory.FILE_ACCESS_OR_PARSING_FAILURE
