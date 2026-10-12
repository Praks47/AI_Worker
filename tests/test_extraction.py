"""Tests for structured field extraction across document formats."""

import json
from pathlib import Path
import openpyxl
import pytest

from agents.extractor import (
    extract_fields,
    process_invoice_request,
    read_document,
)
from agents.models import FailureCategory
from tests.test_readers import (
    DummyOCREngine,
    create_minimal_docx,
    create_minimal_text_pdf,
)


def test_custom_requested_fields(tmp_path: Path):
    txt_file = tmp_path / "invoice.txt"
    txt_file.write_text(
        "Vendor: Initech LLC\nInvoice Number: INI-9981\nInvoice Date: 2026-04-10\nTotal: $1,250.00\n",
        encoding="utf-8",
    )
    doc = read_document(txt_file)

    # Caller only asks for vendor and invoice_number
    result = extract_fields(doc, requested_fields=["vendor", "invoice_number"])
    assert "vendor" in result.fields
    assert "invoice_number" in result.fields
    assert "total_amount" not in result.fields  # Not requested

    vendor_field = result.fields["vendor"]
    assert vendor_field.extracted_value == "Initech LLC"
    assert vendor_field.source_evidence == "Vendor: Initech LLC"
    assert vendor_field.source_location.line_number == 1
    assert vendor_field.extraction_method == "text_regex"

    inv_num_field = result.fields["invoice_number"]
    assert inv_num_field.extracted_value == "INI-9981"
    assert inv_num_field.source_evidence == "Invoice Number: INI-9981"


def test_extract_from_json(tmp_path: Path):
    json_file = tmp_path / "invoice.json"
    data = {
        "company_name": "Umbrella Corp",
        "invoice_no": "UMB-456",
        "issue_date": "2026-03-15",
        "due_date": "2026-04-15",
        "subtotal": 1000.00,
        "tax_amount": 100.00,
        "grand_total": 1100.00,
        "currency": "USD",
    }
    json_file.write_text(json.dumps(data), encoding="utf-8")
    doc = read_document(json_file)

    result = extract_fields(doc)
    assert result.fields["vendor"].extracted_value == "Umbrella Corp"
    assert result.fields["vendor"].extraction_method == "json_key"
    assert result.fields["invoice_number"].extracted_value == "UMB-456"
    assert result.fields["total_amount"].extracted_value == 1100.00
    assert result.fields["subtotal"].extracted_value == 1000.00
    assert result.fields["tax"].extracted_value == 100.00
    assert result.fields["currency"].extracted_value == "USD"


def test_extract_from_xlsx(tmp_path: Path):
    xlsx_file = tmp_path / "invoice.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "InvoiceDetails"
    ws.append(["Vendor:", "Stark Industries"])
    ws.append(["Invoice No:", "STK-1002"])
    ws.append(["Invoice Date:", "2026-06-01"])
    ws.append(["Due Date:", "2026-07-01"])
    ws.append(["Total Amount:", "3500.00"])
    wb.save(xlsx_file)
    doc = read_document(xlsx_file)

    result = extract_fields(doc)
    assert result.fields["vendor"].extracted_value == "Stark Industries"
    assert result.fields["vendor"].extraction_method == "tabular_cell"
    assert result.fields["invoice_number"].extracted_value == "STK-1002"
    assert result.fields["total_amount"].extracted_value == "3500.00"
    assert result.fields["total_amount"].source_location.sheet == "InvoiceDetails"


def test_missing_fields_preserved_explicitly(tmp_path: Path):
    txt_file = tmp_path / "partial_invoice.txt"
    # Invoice has vendor and total, but no invoice_number or due_date
    txt_file.write_text(
        "Vendor: Wayne Enterprises\nInvoice Date: 2026-02-01\nTotal Amount: $750.00\n",
        encoding="utf-8",
    )
    doc = read_document(txt_file)

    result = extract_fields(doc, requested_fields=["vendor", "invoice_number", "due_date", "total_amount"])
    # Successful fields are preserved
    assert result.fields["vendor"].extracted_value == "Wayne Enterprises"
    assert result.fields["total_amount"].extracted_value == "750.00"

    # Missing fields are explicitly None, not fabricated defaults
    assert result.fields["invoice_number"].extracted_value is None
    assert "not found" in result.fields["invoice_number"].unresolved_issue.lower()

    assert result.fields["due_date"].extracted_value is None
    assert "not found" in result.fields["due_date"].unresolved_issue.lower()

    # Failures list tracks missing information
    missing_cats = [f.category for f in result.failures]
    assert FailureCategory.MISSING_INFORMATION in missing_cats


def test_process_invoice_request_single_success(tmp_path: Path):
    pdf_bytes = create_minimal_text_pdf(
        "Acme Corp Invoice\nInvoice #ACM-2026\nDate: 2026-05-10\nTotal: $999.00\nCurrency: USD"
    )
    (tmp_path / "acme_invoice_may.pdf").write_bytes(pdf_bytes)

    result = process_invoice_request(
        data_dir=tmp_path,
        query="Find and read the invoice from Acme Corp",
    )
    assert result.document_path is not None
    assert "acme_invoice_may.pdf" in result.document_path
    assert result.fields["vendor"].extracted_value is not None
    assert result.fields["invoice_number"].extracted_value == "ACM-2026"
    assert result.fields["total_amount"].extracted_value == "999.00"


def test_process_invoice_request_ambiguous(tmp_path: Path):
    (tmp_path / "acme_q1_invoice.txt").write_text("Vendor: Acme Corp\nInvoice: ACM-1\nTotal: $100.00", encoding="utf-8")
    (tmp_path / "acme_q2_invoice.txt").write_text("Vendor: Acme Corp\nInvoice: ACM-2\nTotal: $200.00", encoding="utf-8")

    result = process_invoice_request(
        data_dir=tmp_path,
        query="Find and read the invoice from Acme Corp",
    )
    assert result.status == "ambiguous"
    assert result.requires_human_review is True
    assert len(result.candidate_documents) == 2
    assert any(f.category == FailureCategory.AMBIGUOUS_OR_CONFLICTING_INFORMATION for f in result.failures)


def test_process_invoice_request_not_found(tmp_path: Path):
    (tmp_path / "other_vendor.txt").write_text("Vendor: Other\nTotal: $10.00", encoding="utf-8")

    result = process_invoice_request(
        data_dir=tmp_path,
        query="Find and read the invoice from Cyberdyne Systems",
    )
    assert result.status == "failed"
    assert any(f.category == FailureCategory.MISSING_INFORMATION for f in result.failures)
