"""Pluggable authorization for operator releases.

The signed capability format in this module is deliberately a small, explicit
HMAC envelope rather than a general JWT parser.  It has one supported format,
algorithm, issuer, audience, and key lookup path, and never logs credentials.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mycelium.storage.atomic_state import AtomicStateBackend, InMemoryAtomicStateBackend

CAPABILITY_TOKEN_VERSION = 1
CAPABILITY_SCHEMA_VERSION = 1
CAPABILITY_ALGORITHM = "HS256"
CAPABILITY_PREFIX = "mcap1"


@dataclass(frozen=True)
class OperatorReleaseRequest:
    """The security-relevant fields presented to an operator authorizer."""

    operator_id: str
    request_id: str
    tool: str
    verified: str
    effect_id: str | None = None
    tenant: str | None = None
    policy_version: str | None = None


@runtime_checkable
class OperatorAuthorizer(Protocol):
    """Host-supplied policy hook for an operator release.

    Implementations return ``True`` only when ``credential`` authenticates the
    claimed operator and that operator may perform this exact release.
    """

    def authorize_release(
        self,
        request: OperatorReleaseRequest,
        *,
        credential: str | None,
    ) -> bool: ...


class StaticTokenOperatorAuthorizer:
    """Small-deployment authorizer backed by one secret token per operator.

    Keep tokens outside source control (for example in environment variables or
    a secret manager). This is intentionally a simple bridge, not an SSO or IAM
    system. Tokens identify their owner but are not independently scoped or
    short-lived.
    """

    def __init__(self, tokens: Mapping[str, str]) -> None:
        if not tokens:
            raise ValueError("at least one operator token is required")
        normalized: dict[str, str] = {}
        for operator_id, token in tokens.items():
            if not operator_id or not token:
                raise ValueError("operator ids and tokens must be non-empty")
            normalized[str(operator_id)] = str(token)
        self._tokens = normalized

    def authorize_release(
        self,
        request: OperatorReleaseRequest,
        *,
        credential: str | None,
    ) -> bool:
        expected = self._tokens.get(request.operator_id)
        if expected is None or credential is None:
            return False
        return hmac.compare_digest(expected, credential)


class SignedOperatorReleaseCapabilityAuthorizer:
    """Verify short-lived, single-use, scoped signed release capabilities.

    ``mint_capability`` is intentionally an explicit host-side API.  Callers
    should keep the returned string in a trusted control plane; it must not be
    exposed as an agent/tool input or logged.  Use a durable atomic backend in
    production (Redis/Postgres/file), not the default in-memory backend.
    """

    def __init__(
        self,
        keys: Mapping[str, str],
        *,
        issuer: str,
        audience: str,
        nonce_backend: AtomicStateBackend | None = None,
        nonce_namespace: str = "operator_release_capability_nonce",
        clock: callable = time.time,
    ) -> None:
        if not keys or not issuer or not audience:
            raise ValueError("keys, issuer, and audience are required")
        if any(not str(k) or not str(v) for k, v in keys.items()):
            raise ValueError("capability key ids and secrets must be non-empty")
        self._keys = {str(k): str(v) for k, v in keys.items()}
        self.issuer = issuer
        self.audience = audience
        self._nonces = nonce_backend or InMemoryAtomicStateBackend()
        self._namespace = nonce_namespace
        self._clock = clock

    def mint_capability(
        self,
        request: OperatorReleaseRequest,
        *,
        key_id: str,
        expires_at: float,
        not_before: float | None = None,
        nonce: str | None = None,
    ) -> str:
        """Mint a capability from a trusted host context."""
        secret = self._keys.get(key_id)
        if secret is None:
            raise ValueError("unknown capability key id")
        if not all(
            (
                request.operator_id,
                request.request_id,
                request.tool,
                request.verified,
                request.effect_id,
                request.tenant,
                request.policy_version,
            )
        ):
            raise ValueError("signed capabilities require complete release scope")
        now = self._clock()
        if expires_at <= now or (not_before is not None and expires_at <= not_before):
            raise ValueError("capability expiration must be in the future and after not-before")
        claims = {
            "token_version": CAPABILITY_TOKEN_VERSION,
            "schema_version": CAPABILITY_SCHEMA_VERSION,
            "alg": CAPABILITY_ALGORITHM,
            "kid": key_id,
            "iss": self.issuer,
            "aud": self.audience,
            "sub": request.operator_id,
            "request_id": request.request_id,
            "effect_id": request.effect_id,
            "tool": request.tool,
            "tenant": request.tenant,
            "resolution": request.verified,
            "policy_version": request.policy_version,
            "exp": float(expires_at),
            "nbf": float(now if not_before is None else not_before),
            "nonce": nonce or uuid.uuid4().hex,
            "max_uses": 1,
        }
        return self._encode(claims, secret)

    def authorize_release(self, request: OperatorReleaseRequest, *, credential: str | None) -> bool:
        try:
            claims = self._decode(credential)
            required = (
                "sub",
                "request_id",
                "effect_id",
                "tool",
                "tenant",
                "resolution",
                "policy_version",
                "exp",
                "nbf",
                "nonce",
                "max_uses",
                "iss",
                "aud",
                "kid",
                "alg",
            )
            if any(name not in claims for name in required):
                return False
            if (
                claims["iss"] != self.issuer
                or claims["aud"] != self.audience
                or claims["alg"] != CAPABILITY_ALGORITHM
                or type(claims["max_uses"]) is not int
                or claims["max_uses"] != 1
            ):
                return False
            now = self._clock()
            if not isinstance(claims["exp"], (int, float)) or not isinstance(
                claims["nbf"], (int, float)
            ):
                return False
            if now >= float(claims["exp"]) or now < float(claims["nbf"]):
                return False
            if any(
                not isinstance(claims[name], str) or not claims[name]
                for name in (
                    "sub",
                    "request_id",
                    "effect_id",
                    "tool",
                    "tenant",
                    "resolution",
                    "policy_version",
                    "nonce",
                )
            ):
                return False
            expected = {
                "sub": request.operator_id,
                "request_id": request.request_id,
                "effect_id": request.effect_id,
                "tool": request.tool,
                "tenant": request.tenant,
                "resolution": request.verified,
                "policy_version": request.policy_version,
            }
            if any(claims[name] != value for name, value in expected.items()):
                return False
            # Atomic create is the one-shot consume. It occurs only after every
            # token and scope claim has passed validation.
            return self._nonces.create(
                self._namespace,
                claims["nonce"],
                {
                    "token_version": CAPABILITY_TOKEN_VERSION,
                    "schema_version": CAPABILITY_SCHEMA_VERSION,
                    "used_at": now,
                },
            )
        except Exception:
            return False

    def _encode(self, claims: dict[str, object], secret: str) -> str:
        payload = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        signing = f"{CAPABILITY_PREFIX}.{payload}"
        signature = hmac.new(secret.encode(), signing.encode(), hashlib.sha256).hexdigest()
        return f"{signing}.{signature}"

    def _decode(self, token: str | None) -> dict[str, object]:
        if not isinstance(token, str):
            raise ValueError("malformed capability")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != CAPABILITY_PREFIX:
            raise ValueError("malformed capability")
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(raw)
        if not isinstance(claims, dict):
            raise ValueError("malformed capability")
        if (
            claims.get("token_version") != CAPABILITY_TOKEN_VERSION
            or claims.get("schema_version") != CAPABILITY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported capability version")
        kid = claims.get("kid")
        secret = self._keys.get(kid) if isinstance(kid, str) else None
        if secret is None:
            raise ValueError("unknown capability key")
        expected = hmac.new(
            secret.encode(), f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, parts[2]):
            raise ValueError("invalid capability signature")
        return claims


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


__all__ = [
    "OperatorAuthorizer",
    "OperatorReleaseRequest",
    "StaticTokenOperatorAuthorizer",
    "SignedOperatorReleaseCapabilityAuthorizer",
]
