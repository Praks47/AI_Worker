"""HTTP Client for the Finance and Document APIs."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
import httpx

from agents.reliability import classify_http_status


class FinanceClient:
    """Client for interacting with the company environment HTTP API.

    Supports either an in-process FastAPI TestClient or an httpx.Client
    connected to a live server URL.
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        base_url: str = "http://127.0.0.1:8000",
        timeout_s: float = 10.0,
    ):
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(base_url=base_url, timeout=timeout_s)
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client and hasattr(self._client, "close"):
            self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        clean_params = (
            {k: v for k, v in params.items() if v is not None}
            if params
            else None
        )
        resp = self._client.request(
            method,
            path,
            params=clean_params,
            json=json_body,
            headers=headers,
        )
        if resp.status_code >= 400:
            body_text = ""
            try:
                body_text = resp.text
            except Exception:
                body_text = str(resp.status_code)
            raise classify_http_status(resp.status_code, body_text)
        return resp.json()

    # ------------------------------------------------------------------
    # Document APIs
    # ------------------------------------------------------------------
    def search_documents(
        self, q: Optional[str] = None, folder: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        data = self._request("GET", "/documents/search", params={"q": q, "folder": folder})
        return data.get("items", data.get("results", []))

    def get_document(self, document_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/documents/{document_id}")

    # ------------------------------------------------------------------
    # Invoice APIs
    # ------------------------------------------------------------------
    def list_invoices(
        self,
        vendor: Optional[str] = None,
        invoice_number: Optional[str] = None,
        status: Optional[str] = None,
        q: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        data = self._request(
            "GET",
            "/invoices",
            params={
                "vendor": vendor,
                "invoice_number": invoice_number,
                "status": status,
                "q": q,
            },
        )
        return data.get("items", data.get("invoices", []))

    def get_invoice(self, record_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/invoices/{record_id}")

    def create_invoice(
        self,
        vendor: str,
        invoice_number: str,
        amount: float,
        due_date: str,
        currency: str = "INR",
        source_document: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers: Dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        payload = {
            "vendor": vendor,
            "invoice_number": invoice_number,
            "amount": float(amount),
            "currency": currency,
            "due_date": due_date,
            "source_document": source_document,
        }
        return self._request("POST", "/invoices", json_body=payload, headers=headers)

    def update_invoice(self, record_id: str, **fields: Any) -> Dict[str, Any]:
        return self._request("PATCH", f"/invoices/{record_id}", json_body=fields)

    # ------------------------------------------------------------------
    # Payment APIs
    # ------------------------------------------------------------------
    def list_payments(self, record_id: Optional[str] = None) -> List[Dict[str, Any]]:
        data = self._request("GET", "/payments", params={"record_id": record_id})
        return data.get("items", data.get("payments", []))

    def get_payment(self, payment_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/payments/{payment_id}")

    def create_payment(
        self,
        record_id: str,
        amount: float,
        approval_token: Optional[str] = None,
        approver_role: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers: Dict[str, str] = {}
        if approval_token:
            headers["X-Approval-Token"] = approval_token
        if approver_role:
            headers["X-Approver-Role"] = approver_role
        payload: Dict[str, Any] = {
            "record_id": record_id,
            "amount": float(amount),
        }
        if approval_token:
            payload["approval_token"] = approval_token
        if approver_role:
            payload["approver_role"] = approver_role
        return self._request("POST", "/payments", json_body=payload, headers=headers)


