"""Fault injection with realistic failure *semantics*.

The two phases are deliberately different, because they demand different
agent behaviour:

* ``before_commit`` - the request is rejected before the application touches the
  database. State is unchanged; retrying is safe.
* ``after_commit``  - the application fully processes the request (DB write is
  committed) but the response is lost and the client sees an error/timeout.
  State HAS changed; a blind retry would create a duplicate.
"""
from __future__ import annotations

import re
import threading
from typing import Literal

from pydantic import BaseModel, Field


class FaultSpec(BaseModel):
    method: str = Field(default="*", description="HTTP method or '*'")
    path: str = Field(description="Regex, full-matched against the request path, e.g. '/invoices' or '/invoices/INV-.*'")
    phase: Literal["before_commit", "after_commit"] = "before_commit"
    status_code: int = Field(default=504, ge=400, le=599)
    detail: str = "upstream request timed out"
    times: int | None = Field(default=1, ge=1, description="How many times to fire; null = unlimited")
    skip: int = Field(default=0, ge=0, description="Let this many matching requests through first")
    delay_s: float = Field(default=0.0, ge=0, le=30, description="Sleep before returning the error")


class FaultState:
    def __init__(self, fault_id: str, spec: FaultSpec):
        self.fault_id, self.spec = fault_id, spec
        self.seen = 0      # matching requests observed
        self.fired = 0     # times the fault was actually applied

    def to_dict(self) -> dict:
        return {"fault_id": self.fault_id, **self.spec.model_dump(), "seen": self.seen, "fired": self.fired}


class FaultRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._faults: list[FaultState] = []
        self._counter = 0

    def add(self, spec: FaultSpec) -> FaultState:
        re.compile(spec.path)  # fail fast on a bad regex
        with self._lock:
            self._counter += 1
            state = FaultState(f"fault-{self._counter}", spec)
            self._faults.append(state)
            return state

    def clear(self) -> None:
        with self._lock:
            self._faults.clear()

    def list(self) -> list[dict]:
        with self._lock:
            return [f.to_dict() for f in self._faults]

    def match(self, method: str, path: str) -> FaultState | None:
        """Return the fault to apply to this request (consuming one use), if any."""
        with self._lock:
            for f in self._faults:
                s = f.spec
                if s.method != "*" and s.method.upper() != method.upper():
                    continue
                if not re.fullmatch(s.path, path):
                    continue
                f.seen += 1
                if f.seen <= s.skip:
                    continue
                if s.times is not None and f.fired >= s.times:
                    continue
                f.fired += 1
                return f
        return None


registry = FaultRegistry()
