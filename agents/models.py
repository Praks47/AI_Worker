"""Unified data models for document reading, extraction, verification, reliability, and governed workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class FailureCategory(str, Enum):
    """Failure classification taxonomy across extraction, API, and workflow layers."""
    UNSUPPORTED_FORMAT = "unsupported_format"
    FILE_ACCESS_OR_PARSING_FAILURE = "file_access_or_parsing_failure"
    TRANSIENT_INFRASTRUCTURE_FAILURE = "transient_infrastructure_failure"
    POST_COMMIT_TIMEOUT = "post_commit_timeout"
    OCR_OR_TEXT_QUALITY_FAILURE = "ocr_or_text_quality_failure"
    MISSING_INFORMATION = "missing_information"
    AMBIGUOUS_OR_CONFLICTING_INFORMATION = "ambiguous_or_conflicting_information"
    VERIFICATION_FAILURE = "verification_failure"
    DUPLICATE_RECORD = "duplicate_record"
    GOVERNANCE_BLOCKED = "governance_blocked"


class VerificationStatus(str, Enum):
    """Verification status for extracted fields and operations."""
    VERIFIED = "verified"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class TaskStatus(str, Enum):
    """High-level task execution status."""
    COMPLETED = "completed"
    NEEDS_CLARIFICATION = "needs_clarification"
    NEEDS_APPROVAL = "needs_approval"
    ESCALATED = "escalated"
    FAILED = "failed"


class CommitUncertainty(str, Enum):
    """Commit state classification for fault recovery."""
    DEFINITELY_NOT_COMMITTED = "definitely_not_committed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    COMMITTED = "committed"


@dataclass
class SourceLocation:
    """Precise source location within a document."""
    page: Optional[int] = None
    sheet: Optional[str] = None
    cell: Optional[str] = None
    line_number: Optional[int] = None
    span: Optional[Tuple[int, int]] = None
    description: Optional[str] = None

    def to_string(self) -> str:
        parts: List[str] = []
        if self.page is not None:
            parts.append(f"Page {self.page}")
        if self.sheet is not None:
            parts.append(f"Sheet '{self.sheet}'")
        if self.cell is not None:
            parts.append(f"Cell {self.cell}")
        if self.line_number is not None:
            parts.append(f"Line {self.line_number}")
        if self.span is not None:
            parts.append(f"Span {self.span[0]}:{self.span[1]}")
        if self.description:
            parts.append(self.description)
        return ", ".join(parts) if parts else "Unknown location"


@dataclass
class DocumentPage:
    """Represents a single page in a document (e.g. PDF)."""
    page_num: int
    text: str = ""
    is_ocr: bool = False
    image_count: int = 0
    images: List[Any] = field(default_factory=list)


@dataclass
class DocumentContent:
    """Extracted raw content and structure from a document file."""
    file_path: Path
    file_format: str
    raw_text: str = ""
    pages: List[DocumentPage] = field(default_factory=list)
    tables: List[Dict[str, Any]] = field(default_factory=list)
    structured_data: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedField:
    """Structured representation of an extracted field preserving source evidence."""
    field_name: str
    extracted_value: Any = None
    normalized_value: Any = None
    source_document: str = ""
    source_location: Optional[SourceLocation] = None
    source_evidence: Optional[str] = None
    extraction_method: str = "direct"
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    unresolved_issue: Optional[str] = None
    confidence: float = 1.0


@dataclass
class FailureRecord:
    """Structured failure record."""
    category: FailureCategory
    message: str
    field_name: Optional[str] = None
    file_path: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionResult:
    """Final result of candidate discovery, document parsing, extraction, and verification."""
    document_path: Optional[str] = None
    candidate_documents: List[str] = field(default_factory=list)
    fields: Dict[str, ExtractedField] = field(default_factory=dict)
    failures: List[FailureRecord] = field(default_factory=list)
    status: str = "success"  # "success", "partial", "ambiguous", "failed"
    requires_human_review: bool = False
    summary_notes: List[str] = field(default_factory=list)
    recovery_log: List[str] = field(default_factory=list)

    def add_failure(
        self,
        category: FailureCategory,
        message: str,
        field_name: Optional[str] = None,
        file_path: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.failures.append(
            FailureRecord(
                category=category,
                message=message,
                field_name=field_name,
                file_path=file_path,
                details=details or {},
            )
        )
        if category in (FailureCategory.AMBIGUOUS_OR_CONFLICTING_INFORMATION,):
            self.status = "ambiguous"
            self.requires_human_review = True
        elif category in (
            FailureCategory.UNSUPPORTED_FORMAT,
            FailureCategory.FILE_ACCESS_OR_PARSING_FAILURE,
            FailureCategory.TRANSIENT_INFRASTRUCTURE_FAILURE,
            FailureCategory.OCR_OR_TEXT_QUALITY_FAILURE,
        ) and not self.fields:
            self.status = "failed"
            self.requires_human_review = True
        elif category == FailureCategory.MISSING_INFORMATION and not self.fields:
            self.status = "failed"
        elif self.fields:
            self.status = "partial"
            self.requires_human_review = True


@dataclass
class OrchestratorResponse:
    """Unified response contract returned by the Autonomous AI Task Worker."""
    task_id: str
    status: TaskStatus
    success: bool
    summary: str
    answer: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    records_created: List[Dict[str, Any]] = field(default_factory=list)
    payments_created: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[FailureRecord] = field(default_factory=list)
    audit_log: List[str] = field(default_factory=list)
    clarification_prompt: Optional[str] = None
    clarification_options: List[str] = field(default_factory=list)
    approval_required: bool = False
    approval_token: Optional[str] = None
    approval_details: Optional[Dict[str, Any]] = field(default=None)
    execution_trace: Optional[Dict[str, Any]] = None
