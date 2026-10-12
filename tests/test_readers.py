"""Tests for independent reading and parsing across all supported document formats."""

import base64
import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any
import openpyxl
import pytest

from agents.extractor import (
    discover_files,
    find_candidate_documents,
    read_csv,
    read_document,
    read_docx,
    read_image,
    read_json,
    read_pdf,
    read_txt,
    read_xlsx,
)

MINIMAL_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class DummyOCREngine:
    """Mock OCR engine for deterministic test assertions."""
    def __init__(self, return_text: str = "INVOICE #9988\nTotal: $120.00"):
        self.return_text = return_text
        self.calls = 0

    def extract_text(self, image: Any) -> str:
        self.calls += 1
        return self.return_text


def create_minimal_text_pdf(text: str) -> bytes:
    """Generate a minimal valid PDF byte string containing text."""
    lines = text.splitlines() or [text]
    ops = ["BT", "/F1 12 Tf", "72 712 Td"]
    for idx, line in enumerate(lines):
        escaped = line.replace("(", r"\(").replace(")", r"\)")
        if idx > 0:
            ops.append("0 -16 Td")
        ops.append(f"({escaped}) Tj")
    ops.append("ET")
    stream_content = "\n".join(ops)
    stream_len = len(stream_content.encode("latin-1"))
    pdf = f"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj
4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
5 0 obj << /Length {stream_len} >> stream
{stream_content}
endstream
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000227 00000 n 
0000000298 00000 n 
trailer << /Size 6 /Root 1 0 R >>
startxref
450
%%EOF"""
    return pdf.encode("latin-1")


def create_minimal_image_pdf() -> bytes:
    """Generate a minimal valid PDF byte string containing an image XObject and no text."""
    img_data = b"\xff\x00\x00\xff\x00\x00\xff\x00\x00\xff\x00\x00"  # 2x2 RGB red pixels
    pdf = f"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >> endobj
4 0 obj << /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Length 12 >> stream
{img_data.decode("latin-1")}endstream
endobj
5 0 obj << /Length 20 >> stream
q 100 0 0 100 0 0 cm /Im1 Do Q
endstream
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000223 00000 n 
0000000366 00000 n 
trailer << /Size 6 /Root 1 0 R >>
startxref
440
%%EOF"""
    return pdf.encode("latin-1")


def create_minimal_mixed_pdf(page1_text: str) -> bytes:
    """Generate a 2-page PDF: Page 1 text, Page 2 image XObject."""
    stream1 = f"BT\n/F1 12 Tf\n72 712 Td\n({page1_text}) Tj\nET"
    stream1_len = len(stream1.encode("latin-1"))
    img_data = b"\x00\xff\x00\x00\xff\x00\x00\xff\x00\x00\xff\x00"
    pdf = f"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj
4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
5 0 obj << /Length {stream1_len} >> stream
{stream1}
endstream
endobj
6 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /XObject << /Im1 7 0 R >> >> /Contents 8 0 R >> endobj
7 0 obj << /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Length 12 >> stream
{img_data.decode("latin-1")}endstream
endobj
8 0 obj << /Length 20 >> stream
q 100 0 0 100 0 0 cm /Im1 Do Q
endstream
endobj
xref
0 9
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000122 00000 n 
0000000234 00000 n 
0000000305 00000 n 
0000000400 00000 n 
0000000510 00000 n 
0000000650 00000 n 
trailer << /Size 9 /Root 1 0 R >>
startxref
720
%%EOF"""
    return pdf.encode("latin-1")


def create_minimal_docx(text_paragraphs: list[str]) -> bytes:
    """Generate a minimal docx file as in-memory zip bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '</Types>',
        )
        p_xml = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in text_paragraphs)
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{p_xml}</w:body>"
            "</w:document>",
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_read_txt(tmp_path: Path):
    txt_file = tmp_path / "invoice.txt"
    txt_file.write_text("Vendor: Acme Corp\nInvoice: INV-100\nTotal: $500.00", encoding="utf-8")

    doc = read_document(txt_file)
    assert doc.file_format == "txt"
    assert "Acme Corp" in doc.raw_text
    assert "INV-100" in doc.raw_text
    assert len(doc.pages) == 1
    assert doc.pages[0].is_ocr is False


def test_read_json(tmp_path: Path):
    json_file = tmp_path / "invoice.json"
    data = {
        "vendor": "Globex Inc",
        "invoice_number": "GLX-99",
        "total": 1250.50,
        "currency": "USD",
    }
    json_file.write_text(json.dumps(data), encoding="utf-8")

    doc = read_document(json_file)
    assert doc.file_format == "json"
    assert doc.structured_data == data
    assert "Globex Inc" in doc.raw_text
    assert doc.pages[0].is_ocr is False


def test_read_csv(tmp_path: Path):
    csv_file = tmp_path / "invoice.csv"
    csv_file.write_text("Item,Quantity,Unit_Price,Total\nWidget,2,50.00,100.00\nTax,,,10.00\nFinal,,,110.00\n", encoding="utf-8")

    doc = read_document(csv_file)
    assert doc.file_format == "csv"
    assert len(doc.tables) == 1
    assert doc.tables[0]["headers"] == ["Item", "Quantity", "Unit_Price", "Total"]
    assert "Widget" in doc.raw_text
    assert doc.pages[0].is_ocr is False


def test_read_xlsx(tmp_path: Path):
    xlsx_file = tmp_path / "invoice.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Vendor"
    ws["B1"] = "Stark Industries"
    ws["A2"] = "Invoice #"
    ws["B2"] = "STK-77"
    ws["A3"] = "Total"
    ws["B3"] = 4500.00
    wb.save(xlsx_file)

    doc = read_document(xlsx_file)
    assert doc.file_format == "xlsx"
    assert len(doc.tables) >= 1
    assert "Summary!B1" in doc.raw_text
    assert "Stark Industries" in doc.raw_text


def test_read_docx(tmp_path: Path):
    docx_file = tmp_path / "invoice.docx"
    docx_bytes = create_minimal_docx(["Wayne Enterprises Invoice", "Invoice No: WYN-001", "Total: $990.00"])
    docx_file.write_bytes(docx_bytes)

    doc = read_document(docx_file)
    assert doc.file_format == "docx"
    assert "Wayne Enterprises Invoice" in doc.raw_text
    assert "WYN-001" in doc.raw_text


def test_read_image(tmp_path: Path):
    img_file = tmp_path / "invoice.png"
    img_file.write_bytes(MINIMAL_PNG_BYTES)

    dummy_ocr = DummyOCREngine(return_text="Cyberdyne Systems\nInvoice #CY-101\nTotal: $1200.00")
    doc = read_document(img_file, ocr_engine=dummy_ocr)

    assert doc.file_format == "image"
    assert dummy_ocr.calls == 1
    assert "Cyberdyne Systems" in doc.raw_text
    assert doc.pages[0].is_ocr is True


def test_read_pdf_text(tmp_path: Path):
    pdf_file = tmp_path / "invoice_text.pdf"
    pdf_file.write_bytes(create_minimal_text_pdf("Oscorp Technologies Invoice #OSC-500 Total: $850.00"))

    doc = read_document(pdf_file)
    assert doc.file_format == "pdf"
    assert len(doc.pages) == 1
    assert doc.pages[0].is_ocr is False
    assert "Oscorp Technologies" in doc.raw_text


def test_read_pdf_scanned(tmp_path: Path):
    pdf_file = tmp_path / "invoice_scanned.pdf"
    pdf_file.write_bytes(create_minimal_image_pdf())

    dummy_ocr = DummyOCREngine(return_text="Initech LLC\nInvoice: INI-42\nTotal: $340.00")
    doc = read_document(pdf_file, ocr_engine=dummy_ocr)

    assert doc.file_format == "pdf"
    assert len(doc.pages) == 1
    assert doc.pages[0].is_ocr is True
    assert dummy_ocr.calls >= 1
    assert "Initech LLC" in doc.raw_text


def test_read_pdf_mixed(tmp_path: Path):
    pdf_file = tmp_path / "invoice_mixed.pdf"
    pdf_file.write_bytes(create_minimal_mixed_pdf("Page 1 Direct Text From Acme Corp"))

    dummy_ocr = DummyOCREngine(return_text="Page 2 Scanned Content Summary Total: $600.00")
    doc = read_document(pdf_file, ocr_engine=dummy_ocr)

    assert doc.file_format == "pdf"
    assert len(doc.pages) == 2
    # Page 1 had direct text -> is_ocr = False
    assert doc.pages[0].is_ocr is False
    assert "Direct Text From Acme" in doc.pages[0].text
    # Page 2 was scanned -> is_ocr = True
    assert doc.pages[1].is_ocr is True
    assert "Page 2 Scanned Content" in doc.pages[1].text


def test_unsupported_format(tmp_path: Path):
    bad_file = tmp_path / "document.xyz"
    bad_file.write_text("Some random data", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported format"):
        read_document(bad_file)


def test_corrupt_file(tmp_path: Path):
    corrupt_pdf = tmp_path / "broken.pdf"
    corrupt_pdf.write_bytes(b"NOT A REAL PDF CONTENT AT ALL")
    with pytest.raises(ValueError, match="Corrupt or invalid PDF"):
        read_document(corrupt_pdf)


def test_file_discovery_and_filtering(tmp_path: Path):
    (tmp_path / "invoice1.pdf").write_bytes(create_minimal_text_pdf("Sample Invoice 1"))
    (tmp_path / "invoice2.txt").write_text("Sample Invoice 2", encoding="utf-8")
    (tmp_path / "data.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ignored.exe").write_bytes(b"binary")
    (tmp_path / "ignored.pyc").write_bytes(b"bytecode")

    found = discover_files(tmp_path)
    found_names = {f.name for f in found}
    assert "invoice1.pdf" in found_names
    assert "invoice2.txt" in found_names
    assert "data.json" in found_names
    assert "ignored.exe" not in found_names
    assert "ignored.pyc" not in found_names


def test_find_candidate_single_match(tmp_path: Path):
    (tmp_path / "acme_invoice_2026.pdf").write_bytes(create_minimal_text_pdf("Acme Corp Invoice"))
    (tmp_path / "globex_order.txt").write_text("Globex Order Form", encoding="utf-8")

    candidates, is_ambiguous, reason = find_candidate_documents(
        tmp_path, "Find and read the invoice from Acme Corp"
    )
    assert len(candidates) == 1
    assert candidates[0].name == "acme_invoice_2026.pdf"
    assert is_ambiguous is False
    assert reason is None


def test_find_candidate_ambiguous_matches(tmp_path: Path):
    (tmp_path / "acme_january_invoice.pdf").write_bytes(create_minimal_text_pdf("Acme Invoice Jan"))
    (tmp_path / "acme_february_invoice.pdf").write_bytes(create_minimal_text_pdf("Acme Invoice Feb"))

    candidates, is_ambiguous, reason = find_candidate_documents(
        tmp_path, "Find and read the invoice from Acme Corp"
    )
    assert len(candidates) == 2
    assert is_ambiguous is True
    assert "Ambiguous: multiple matching documents" in reason


def test_find_candidate_no_matches(tmp_path: Path):
    (tmp_path / "other_company.txt").write_text("Other Document", encoding="utf-8")

    candidates, is_ambiguous, reason = find_candidate_documents(
        tmp_path, "Find and read the invoice from Wayne Enterprises"
    )
    assert len(candidates) == 0
    assert is_ambiguous is False
    assert "No documents matched" in reason
