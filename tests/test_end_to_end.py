"""End-to-end integration tests for document discovery, reading, extraction, and verification."""

import json
from pathlib import Path
import openpyxl
import pytest

from agents.extractor import process_invoice_request
from agents.models import FailureCategory, VerificationStatus
from tests.test_readers import (
    MINIMAL_PNG_BYTES,
    create_minimal_image_pdf,
    create_minimal_text_pdf,
)
from tests.test_reliability import FlakyOCREngine


def test_e2e_text_pdf_full_pipeline(tmp_path: Path):
    # Set up directory with multiple files including non-invoice and target invoice
    (tmp_path / "notes.txt").write_text("Meeting notes from yesterday", encoding="utf-8")
    (tmp_path / "summary.csv").write_text("Col1,Col2\nVal1,Val2\n", encoding="utf-8")

    pdf_bytes = create_minimal_text_pdf(
        "Vendor: Stark Industries\n"
        "Invoice Number: STK-2026-99\n"
        "Invoice Date: 2026-05-01\n"
        "Due Date: 2026-06-01\n"
        "Subtotal: $4,000.00\n"
        "Tax: $400.00\n"
        "Total Amount: $4,400.00\n"
        "Currency: USD"
    )
    (tmp_path / "stark_industries_invoice.pdf").write_bytes(pdf_bytes)

    result = process_invoice_request(
        data_dir=tmp_path,
        query="Find and read the invoice from Stark Industries",
    )

    # 1. Document discovered & identified
    assert result.document_path is not None
    assert "stark_industries_invoice.pdf" in result.document_path

    # 2. Status & verification
    assert result.status == "success"
    assert result.requires_human_review is False

    # 3. Fields extracted and verified
    assert result.fields["vendor"].extracted_value == "Stark Industries"
    assert result.fields["vendor"].verification_status == VerificationStatus.VERIFIED

    assert result.fields["invoice_number"].extracted_value == "STK-2026-99"
    assert result.fields["invoice_number"].verification_status == VerificationStatus.VERIFIED

    assert result.fields["total_amount"].extracted_value == "4400.00"
    assert result.fields["total_amount"].verification_status == VerificationStatus.VERIFIED

    assert result.fields["currency"].extracted_value == "USD"
    assert result.fields["currency"].verification_status == VerificationStatus.VERIFIED

    # 4. Source grounding preserved
    assert result.fields["invoice_number"].source_evidence is not None
    assert "STK-2026-99" in result.fields["invoice_number"].source_evidence
    assert result.fields["total_amount"].source_location.page == 1


def test_e2e_scanned_pdf_with_ocr(tmp_path: Path):
    pdf_bytes = create_minimal_image_pdf()
    (tmp_path / "wayne_scanned_invoice.pdf").write_bytes(pdf_bytes)

    ocr_text = (
        "Vendor: Wayne Enterprises\n"
        "Invoice Number: WYN-888\n"
        "Invoice Date: 2026-07-15\n"
        "Due Date: 2026-08-15\n"
        "Subtotal: $2,000.00\n"
        "Tax: $200.00\n"
        "Total Amount: $2,200.00\n"
        "Currency: USD"
    )
    ocr_engine = FlakyOCREngine(fail_count=0, return_text=ocr_text)

    result = process_invoice_request(
        data_dir=tmp_path,
        query="Find and read the invoice from Wayne Enterprises",
        ocr_engine=ocr_engine,
    )

    assert result.status == "success"
    assert result.fields["vendor"].extracted_value == "Wayne Enterprises"
    assert result.fields["invoice_number"].extracted_value == "WYN-888"
    assert result.fields["total_amount"].extracted_value == "2200.00"
    assert result.fields["invoice_number"].extraction_method == "ocr_regex"
    assert ocr_engine.calls >= 1


def test_e2e_image_invoice_with_flaky_ocr_recovery(tmp_path: Path):
    img_file = tmp_path / "cyberdyne_invoice.png"
    img_file.write_bytes(MINIMAL_PNG_BYTES)

    ocr_text = (
        "Vendor: Cyberdyne Systems\n"
        "Invoice Number: CYB-100\n"
        "Invoice Date: 2026-08-01\n"
        "Total Amount: $1,500.00\n"
        "Currency: USD"
    )
    # Fails 2 times transiently before succeeding
    ocr_engine = FlakyOCREngine(fail_count=2, return_text=ocr_text)

    result = process_invoice_request(
        data_dir=tmp_path,
        query="Find and read the invoice from Cyberdyne Systems",
        requested_fields=["vendor", "invoice_number", "total_amount", "currency"],
        ocr_engine=ocr_engine,
    )

    assert result.status == "success"
    assert result.fields["vendor"].extracted_value == "Cyberdyne Systems"
    assert result.fields["invoice_number"].extracted_value == "CYB-100"
    assert result.fields["total_amount"].extracted_value == "1500.00"
    assert ocr_engine.calls == 3  # Verified bounded retries


def test_e2e_spreadsheet_xlsx(tmp_path: Path):
    xlsx_file = tmp_path / "globex_billing.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws.append(["Vendor:", "Globex Corporation"])
    ws.append(["Invoice Number:", "GLX-500"])
    ws.append(["Invoice Date:", "2026-09-01"])
    ws.append(["Due Date:", "2026-10-01"])
    ws.append(["Subtotal:", "1000.00"])
    ws.append(["Tax:", "100.00"])
    ws.append(["Total Amount:", "1100.00"])
    ws.append(["Currency:", "USD"])
    wb.save(xlsx_file)

    result = process_invoice_request(
        data_dir=tmp_path,
        query="Find and read the invoice from Globex Corporation",
    )

    assert result.status == "success"
    assert result.fields["vendor"].extracted_value == "Globex Corporation"
    assert result.fields["total_amount"].extracted_value == "1100.00"
    assert result.fields["total_amount"].source_location.sheet == "Invoice"
    assert result.fields["total_amount"].source_location.cell == "B7"


def test_e2e_ambiguity_no_arbitrary_choice(tmp_path: Path):
    # Two Acme invoices in the directory
    (tmp_path / "acme_invoice_01.txt").write_text("Vendor: Acme Corp\nInvoice #01\nTotal: $100.00", encoding="utf-8")
    (tmp_path / "acme_invoice_02.txt").write_text("Vendor: Acme Corp\nInvoice #02\nTotal: $200.00", encoding="utf-8")

    result = process_invoice_request(
        data_dir=tmp_path,
        query="Find and read the invoice from Acme Corp",
    )

    assert result.status == "ambiguous"
    assert result.requires_human_review is True
    assert len(result.candidate_documents) == 2
    assert any(f.category == FailureCategory.AMBIGUOUS_OR_CONFLICTING_INFORMATION for f in result.failures)


def test_e2e_math_inconsistency_requires_human_review(tmp_path: Path):
    # Invoice where Subtotal ($1000) + Tax ($100) != Total ($1500)
    txt_content = (
        "Vendor: Initech LLC\n"
        "Invoice Number: INI-777\n"
        "Invoice Date: 2026-01-10\n"
        "Subtotal: $1,000.00\n"
        "Tax: $100.00\n"
        "Total Amount: $1,500.00\n"  # Contradiction!
    )
    (tmp_path / "initech_invoice.txt").write_text(txt_content, encoding="utf-8")

    result = process_invoice_request(
        data_dir=tmp_path,
        query="Find and read the invoice from Initech LLC",
    )

    # Status must require human review
    assert result.requires_human_review is True
    assert result.status == "partial"
    assert any(f.category == FailureCategory.VERIFICATION_FAILURE for f in result.failures)
    assert result.fields["total_amount"].verification_status == VerificationStatus.NEEDS_HUMAN_REVIEW


def test_e2e_corrupt_file_reporting(tmp_path: Path):
    (tmp_path / "broken_corrupt_invoice.pdf").write_bytes(b"CORRUPTED BYTES")

    result = process_invoice_request(
        data_dir=tmp_path,
        query="Find and read the invoice from Broken",
    )

    assert result.status == "failed"
    assert any(f.category == FailureCategory.FILE_ACCESS_OR_PARSING_FAILURE for f in result.failures)
