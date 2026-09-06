"""Opt-in sandbox provider adapter for cluster verification."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from mycelium.reconcile import ReconcileResult
from mycelium.verify.types import IsolationRefused


@dataclass(frozen=True)
class SandboxProviderConfig:
    base_url: str
    token: str | None
    adapter_name: str
    timeout_seconds: float

    def worker_payload(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "token": self.token,
            "adapter_name": self.adapter_name,
            "timeout_seconds": self.timeout_seconds,
        }


class HttpSandboxProvider:
    """Minimal JSON sandbox contract: PUT and GET ``/operations/{id}``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.base_url = str(payload["base_url"]).rstrip("/")
        self.token = payload.get("token")
        self.timeout = float(payload.get("timeout_seconds", 5.0))

    def _request(
        self, method: str, operation_id: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        url = f"{self.base_url}/operations/{quote(operation_id, safe='')}"
        headers = {"Accept": "application/json", "X-Mycelium-Sandbox": "true"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, sort_keys=True).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310  # noqa: S310 - explicit opt-in URL
                raw = response.read()
        except HTTPError as exc:
            if exc.code == 404 and method == "GET":
                return None
            raise
        return dict(json.loads(raw or b"{}"))

    def execute(self, operation_id: str) -> dict[str, Any]:
        return self._request(
            "PUT",
            operation_id,
            {"operation_id": operation_id, "idempotency_key": operation_id, "sandbox": True},
        ) or {"operation_id": operation_id, "status": "completed"}

    def charge(
        self,
        amount: int,
        *,
        op_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.execute(op_id)
        return {**result, "amount": amount, "idempotency_key": idempotency_key}

    def lookup(self, operation_id: str) -> dict[str, Any] | None:
        return self._request("GET", operation_id)


class SandboxReconciler:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.provider = HttpSandboxProvider(payload)

    def reconcile(self, entry: Any) -> ReconcileResult:
        operation_id = str(getattr(entry, "external_operation_ref", "") or "")
        if not operation_id:
            return ReconcileResult.unknown()
        observed = self.provider.lookup(operation_id)
        if observed is None:
            return ReconcileResult.unknown()
        status = str(observed.get("status", "")).lower()
        if status in {"complete", "completed", "succeeded", "success"}:
            return ReconcileResult.completed(observed)
        return ReconcileResult.unknown()


def load_sandbox_provider_config(raw: dict[str, Any]) -> SandboxProviderConfig:
    if not bool(raw.get("sandbox")):
        raise IsolationRefused("verify.cluster.provider.sandbox must be true")
    if str(raw.get("adapter", "http_json")) != "http_json":
        raise IsolationRefused(
            "cluster verification currently supports provider.adapter: http_json"
        )
    env_name = str(raw.get("base_url_env") or "")
    if not env_name:
        raise IsolationRefused("verify.cluster.provider.base_url_env is required")
    base_url = os.environ.get(env_name, "").strip()
    if not base_url:
        raise IsolationRefused(f"sandbox provider URL environment variable {env_name!r} is empty")
    if not base_url.startswith(("http://", "https://")):
        raise IsolationRefused("sandbox provider URL must use http:// or https://")
    token_env = str(raw.get("token_env") or "")
    token = os.environ.get(token_env) if token_env else None
    return SandboxProviderConfig(
        base_url=base_url,
        token=token,
        adapter_name=str(raw.get("name", "http-json-sandbox")),
        timeout_seconds=float(raw.get("timeout", 5.0)),
    )


__all__ = [
    "HttpSandboxProvider",
    "SandboxProviderConfig",
    "SandboxReconciler",
    "load_sandbox_provider_config",
]
