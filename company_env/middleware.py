"""Request logging + fault injection for the API surface (not the UI/admin)."""
from __future__ import annotations

import asyncio

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from . import db
from .faults import registry

API_PREFIXES = ("/invoices", "/payments", "/documents")


class FaultAndLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith(API_PREFIXES):
            return await call_next(request)

        fault = registry.match(request.method, path)
        if fault is not None and fault.spec.phase == "before_commit":
            response = await self._fault_response(fault)
        else:
            response = await call_next(request)  # handler runs; DB is committed here
            if fault is not None:                # after_commit: throw the real response away
                response = await self._fault_response(fault)

        db.log_request(
            request.method, path, request.url.query, response.status_code,
            fault.fault_id if fault else None, fault.spec.phase if fault else None,
        )
        return response

    @staticmethod
    async def _fault_response(fault):
        if fault.spec.delay_s:
            await asyncio.sleep(fault.spec.delay_s)
        error = "gateway_timeout" if fault.spec.status_code == 504 else "injected_fault"
        return JSONResponse({"error": error, "detail": fault.spec.detail},
                            status_code=fault.spec.status_code)
