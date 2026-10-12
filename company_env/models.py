"""Pydantic request/response models for the finance API."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Currency = Literal["INR", "USD", "EUR"]
InvoiceStatus = Literal["Pending", "Approved", "Paid", "Rejected", "On Hold"]


class InvoiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    vendor: str = Field(min_length=1)
    invoice_number: str = Field(min_length=1)
    amount: float = Field(gt=0)
    currency: Currency = "INR"
    due_date: date
    source_document: str | None = None

    @field_validator("amount")
    @classmethod
    def _round(cls, v: float) -> float:
        return round(v, 2)


class InvoiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    vendor: str | None = Field(default=None, min_length=1)
    invoice_number: str | None = Field(default=None, min_length=1)
    amount: float | None = Field(default=None, gt=0)
    currency: Currency | None = None
    due_date: date | None = None
    source_document: str | None = None
    status: InvoiceStatus | None = None


class InvoiceOut(BaseModel):
    record_id: str
    vendor: str
    invoice_number: str
    amount: float
    currency: str
    due_date: str
    source_document: str | None
    status: str
    created_at: str
    updated_at: str


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    amount: float = Field(gt=0)
    approval_token: str | None = None
    approver_role: str | None = None




class PaymentOut(BaseModel):
    payment_id: str
    record_id: str
    amount: float
    currency: str
    status: str
    created_at: str
