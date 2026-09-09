"""Development-only authenticated HTTP adapter for the authoritative ledger.

This module owns transport validation and identity-v1 encoding only.  Claim,
lease, fence, recovery, and state-transition decisions remain in ActionLedger.
It intentionally has no provider execution or language-specific client API.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import ipaddress
import json
import math
import os
import re
import socket
import stat
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mycelium.action_ledger import ActionLedger
from mycelium.decision import Decision
from mycelium.ledger_storage import FileLedgerStorage
from mycelium.outcome_emit import FileOutcomeStorage, OutcomeEmitter
from mycelium.storage import PostgresLedgerStorage, PostgresOutcomeStorage
from mycelium.transition import (
    RetryPermission,
    SideEffectBoundary,
    SideEffectClass,
    ToolCapability,
    ToolTransitionBinding,
)

PROTOCOL_VERSION = "v1alpha1"
IDENTITY_VERSION = "1"
CANONICALIZATION_VERSION = "jcs-1"
MAX_PREIMAGE_BYTES = 65536
MAX_BODY_BYTES = 1024 * 1024
MAX_PATH_BYTES = 1024
TOKEN_BYTES = 43  # base64url encoding of 256 random bits


def openapi_document() -> dict[str, Any]:
    """Return the deterministic, language-neutral sidecar contract."""

    def ref(name: str) -> dict[str, str]:
        return {"$ref": f"#/components/schemas/{name}"}

    def string(pattern: str | None = None, max_length: int = 1024) -> dict[str, Any]:
        value: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": max_length}
        if pattern:
            value["pattern"] = pattern
        return value

    json_value: dict[str, Any] = {
        "oneOf": [
            {"type": ["string", "boolean", "null"]},
            {"type": "integer", "minimum": -(2**53 - 1), "maximum": 2**53 - 1},
            {"type": "array", "items": {"$ref": "#/components/schemas/JsonValue"}},
            ref("JsonObject"),
            ref("DecimalValue"),
            ref("UrlValue"),
        ],
        "description": "Raw floating-point values are rejected by the engine.",
    }
    identity_properties = {
        "application_id": ref("ApplicationId"),
        "business_request_id": ref("BusinessRequestId"),
        "canonicalization_version": ref("CanonicalizationVersion"),
        "destination": {"anyOf": [ref("Destination"), {"type": "null"}]},
        "execution_scope": ref("ExecutionScope"),
        "identity_version": ref("IdentityVersion"),
        "input": ref("IdentityInput"),
        "tenant_id": ref("TenantId"),
        "tool_contract_version": ref("ToolContractVersion"),
        "tool_id": ref("ToolId"),
        "expected_effect_id": {**ref("EffectId"), "description": "Client assertion only."},
    }
    identity_required = [
        "application_id",
        "business_request_id",
        "canonicalization_version",
        "destination",
        "execution_scope",
        "identity_version",
        "input",
        "tenant_id",
        "tool_contract_version",
        "tool_id",
    ]
    fenced_properties = {"owner_id": ref("OwnerId"), "fence": ref("Fence")}
    schemas: dict[str, Any] = {
        "ProtocolVersion": {"type": "string", "const": PROTOCOL_VERSION},
        "IdentityVersion": {"type": "string", "const": IDENTITY_VERSION},
        "CanonicalizationVersion": {"type": "string", "const": CANONICALIZATION_VERSION},
        "EffectId": {"type": "string", "pattern": r"^mycelium:effect:v1:[0-9a-f]{64}$"},
        "TenantId": string(),
        "ApplicationId": string(),
        "AgentId": string(),
        "BusinessRequestId": string(),
        "ToolId": string(),
        "ToolContractVersion": string(),
        "OwnerId": string(),
        "Fence": {"type": "integer", "minimum": 1, "maximum": 2**63 - 1},
        "JsonValue": json_value,
        "JsonObject": {
            "type": "object",
            "properties": {"$type": {"type": "string"}},
            "additionalProperties": ref("JsonValue"),
            "not": {
                "required": ["$type"],
                "properties": {"$type": {"type": "string"}},
            },
        },
        "IdentityInput": {"$ref": "#/components/schemas/JsonValue"},
        "Destination": {"$ref": "#/components/schemas/JsonObject"},
        "ExecutionScope": {"$ref": "#/components/schemas/JsonObject"},
        "DecimalValue": {
            "type": "object",
            "additionalProperties": False,
            "required": ["$type", "profile", "value"],
            "properties": {
                "$type": {"const": "decimal"},
                "profile": {"const": "decimal-1"},
                "value": {
                    "type": "string",
                    "pattern": r"^(0|-?[1-9][0-9]*)(\.[0-9]*[1-9])?$",
                    "maxLength": 40,
                },
            },
            "description": "Engine additionally enforces 38 total digits and scale 18.",
        },
        "UrlValue": {
            "type": "object",
            "additionalProperties": False,
            "required": ["$type", "profile", "value"],
            "properties": {
                "$type": {"const": "url"},
                "profile": {"const": "url-1"},
                "value": {"type": "string", "minLength": 1, "maxLength": 4096},
            },
            "description": "Engine enforces the absolute HTTP(S), credential-free url-1 profile.",
        },
        "Decision": {
            "type": "object",
            "additionalProperties": False,
            "required": ["allowed", "verdicts", "denied_reasons"],
            "properties": {
                "allowed": {"type": "boolean"},
                "verdicts": {"type": "array", "items": ref("Verdict")},
                "denied_reasons": {"type": "array", "items": {"type": "string"}},
            },
        },
        "Verdict": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "allowed"],
            "properties": {
                "name": string(),
                "allowed": {"type": "boolean"},
                "reason": {"type": ["string", "null"]},
            },
        },
        "BoundaryState": {"type": "string", "enum": ["not_crossed", "maybe_crossed", "crossed"]},
        "EffectState": {
            "type": "string",
            "enum": ["INTENDED", "ATTEMPTING", "COMMITTED", "ABORTED", "UNKNOWN"],
        },
        "Lease": {
            "type": "object",
            "additionalProperties": False,
            "required": ["leased_until", "last_heartbeat_at"],
            "properties": {
                "leased_until": {"type": ["number", "null"]},
                "last_heartbeat_at": {"type": ["number", "null"]},
            },
        },
        "EffectHandle": {
            "type": "object",
            "additionalProperties": False,
            "required": ["effect_id", "owner_id", "fence"],
            "properties": {
                "effect_id": ref("EffectId"),
                "owner_id": ref("OwnerId"),
                "fence": ref("Fence"),
            },
        },
        "ProviderReference": {
            "type": "object",
            "additionalProperties": False,
            "required": ["provider_operation_ref"],
            "properties": {"provider_operation_ref": string()},
        },
        "RetryClassification": {"type": "string", "enum": ["retryable", "caller_action_required"]},
        "ClaimDisposition": {
            "type": "string",
            "enum": [
                "EXECUTE",
                "RETURN_STORED_RESULT",
                "WAIT_FOR_OWNER",
                "RECORD_DECISION",
                "UNKNOWN",
                "DENIED",
                "TERMINAL_ABORTED",
            ],
        },
        "HealthReply": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "protocol_version"],
            "properties": {"status": {"const": "ok"}, "protocol_version": ref("ProtocolVersion")},
        },
        "CapabilitiesReply": {
            "type": "object",
            "required": [
                "protocol_version",
                "identity_namespace",
                "capabilities",
                "operations",
                "development_only",
            ],
            "properties": {
                "protocol_version": ref("ProtocolVersion"),
                "identity_namespace": {"const": "identity-v1"},
                "capabilities": {"type": "array", "items": {"type": "string"}},
                "operations": {"type": "array", "items": {"type": "string"}},
                "development_only": {"const": True},
            },
            "additionalProperties": True,
        },
        "DeriveIdentityRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": identity_required,
            "properties": identity_properties,
        },
        "DeriveIdentityReply": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "protocol_version",
                "effect_id",
                "canonical_json",
                "canonical_bytes",
                "identity_namespace",
            ],
            "properties": {
                "protocol_version": ref("ProtocolVersion"),
                "effect_id": ref("EffectId"),
                "canonical_json": {"type": "string"},
                "canonical_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_PREIMAGE_BYTES},
                "identity_namespace": {"const": "identity-v1"},
            },
        },
        "ClaimEffectRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": identity_required,
            "properties": {
                **identity_properties,
                "decision": ref("Decision"),
                "lease_ttl": {"type": "number", "exclusiveMinimum": 0},
            },
        },
        "EffectInspectionReply": {
            "type": "object",
            "unevaluatedProperties": False,
            "required": [
                "protocol_version",
                "effect_id",
                "effect_state",
                "terminal_outcome",
                "owner_id",
                "lease",
                "fence",
                "provider_boundary",
                "provider_operation_ref",
                "result",
                "decision",
                "error",
            ],
            "properties": {
                "protocol_version": ref("ProtocolVersion"),
                "effect_id": ref("EffectId"),
                "effect_state": ref("EffectState"),
                "terminal_outcome": {"type": ["string", "null"]},
                "owner_id": {"anyOf": [ref("OwnerId"), {"type": "null"}]},
                "lease": ref("Lease"),
                "fence": {"anyOf": [ref("Fence"), {"type": "null"}]},
                "provider_boundary": {"anyOf": [ref("BoundaryState"), {"type": "null"}]},
                "provider_operation_ref": {"type": ["string", "null"]},
                "result": {},
                "decision": {"anyOf": [ref("Decision"), {"type": "null"}]},
                "error": {"type": ["string", "null"]},
                "disposition": ref("ClaimDisposition"),
            },
        },
        "RenewLeaseRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": [*identity_required, "owner_id", "fence"],
            "properties": {
                **identity_properties,
                **fenced_properties,
                "lease_ttl": {"type": "number", "exclusiveMinimum": 0},
            },
        },
        "RecordBoundaryRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": [*identity_required, "owner_id", "fence", "boundary"],
            "properties": {
                **identity_properties,
                **fenced_properties,
                "boundary": ref("BoundaryState"),
            },
        },
        "AttachProviderReferenceRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": [*identity_required, "owner_id", "fence", "provider_operation_ref"],
            "properties": {
                **identity_properties,
                **fenced_properties,
                "provider_operation_ref": string(),
            },
        },
        "CompleteEffectRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": [*identity_required, "owner_id", "fence"],
            "properties": {**identity_properties, **fenced_properties, "result": {}},
        },
        "FailEffectRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": [*identity_required, "owner_id", "fence"],
            "properties": {
                **identity_properties,
                **fenced_properties,
                "boundary": ref("BoundaryState"),
            },
        },
        "ReconcileEffectRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": identity_required,
            "properties": identity_properties,
        },
        "ProtocolError": {
            "type": "object",
            "additionalProperties": False,
            "required": ["protocol_version", "error"],
            "properties": {
                "protocol_version": ref("ProtocolVersion"),
                "error": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "code",
                        "message",
                        "retryable",
                        "caller_action_required",
                        "state_may_have_changed",
                        "effect_may_have_happened",
                    ],
                    "properties": {
                        "code": {
                            "type": "string",
                            "enum": [
                                "INVALID_REQUEST",
                                "INVALID_JSON",
                                "DUPLICATE_OBJECT_KEY",
                                "REQUEST_TOO_LARGE",
                                "RESPONSE_TOO_LARGE",
                                "NOT_FOUND",
                                "AUTHENTICATION_REQUIRED",
                                "AUTHENTICATION_INVALID",
                                "TENANT_MISMATCH",
                                "APPLICATION_MISMATCH",
                                "IDENTITY_REQUIRED",
                                "EFFECT_ID_MISMATCH",
                                "UNSUPPORTED_IDENTITY_VERSION",
                                "UNSUPPORTED_CANONICALIZATION_VERSION",
                                "IDENTITY_PREIMAGE_TOO_LARGE",
                                "UNSUPPORTED_VALUE_TYPE",
                                "NON_FINITE_NUMBER",
                                "NON_CANONICAL_NUMBER",
                                "INTEGER_OUT_OF_RANGE",
                                "INVALID_UNICODE",
                                "NON_CANONICAL_DECIMAL",
                                "INVALID_DECIMAL",
                                "INVALID_URL",
                                "OWNER_REQUIRED",
                                "FENCE_REQUIRED",
                                "ACTIVE_OWNER",
                                "STALE_FENCE",
                                "LEASE_LOST",
                                "INVALID_TRANSITION",
                                "POLICY_DENIED",
                                "RECONCILIATION_UNAVAILABLE",
                                "INTERNAL_PROTOCOL_ERROR",
                            ],
                        },
                        "message": {"type": "string"},
                        "retryable": {"type": "boolean"},
                        "caller_action_required": {"type": "boolean"},
                        "state_may_have_changed": {"type": "boolean"},
                        "effect_may_have_happened": {"type": "boolean"},
                        "effect_id": ref("EffectId"),
                        "details": {"type": "object"},
                    },
                },
            },
        },
    }
    for response_name in (
        "RenewLeaseReply",
        "RecordBoundaryReply",
        "AttachProviderReferenceReply",
        "CompleteEffectReply",
        "FailEffectReply",
    ):
        schemas[response_name] = {"$ref": "#/components/schemas/EffectInspectionReply"}
    projection = ref("EffectInspectionReply")
    disposition_schemas = {}
    for name, value in (
        ("ExecuteDisposition", "EXECUTE"),
        ("StoredResultDisposition", "RETURN_STORED_RESULT"),
        ("WaitForOwnerDisposition", "WAIT_FOR_OWNER"),
        ("RecordDecisionDisposition", "RECORD_DECISION"),
        ("UnknownDisposition", "UNKNOWN"),
        ("DeniedDisposition", "DENIED"),
        ("TerminalAbortedDisposition", "TERMINAL_ABORTED"),
    ):
        disposition_schemas[name] = {
            "allOf": [
                projection,
                {
                    "type": "object",
                    "required": ["disposition"],
                    "properties": {"disposition": {"const": value}},
                },
            ]
        }
    disposition_schemas["ExecuteDisposition"]["allOf"][1]["required"] = [
        "disposition",
        "owner_id",
        "fence",
        "lease",
    ]
    schemas.update(disposition_schemas)
    claim_union = {
        "oneOf": [ref(name) for name in disposition_schemas],
        "discriminator": {
            "propertyName": "disposition",
            "mapping": {
                value: f"#/components/schemas/{name}"
                for name, value in (
                    ("ExecuteDisposition", "EXECUTE"),
                    ("StoredResultDisposition", "RETURN_STORED_RESULT"),
                    ("WaitForOwnerDisposition", "WAIT_FOR_OWNER"),
                    ("RecordDecisionDisposition", "RECORD_DECISION"),
                    ("UnknownDisposition", "UNKNOWN"),
                    ("DeniedDisposition", "DENIED"),
                    ("TerminalAbortedDisposition", "TERMINAL_ABORTED"),
                )
            },
        },
    }
    schemas["ClaimEffectReply"] = claim_union
    schemas["EffectHandle"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["effect_id", "owner_id", "fence"],
        "properties": {
            "effect_id": ref("EffectId"),
            "owner_id": ref("OwnerId"),
            "fence": ref("Fence"),
        },
    }

    def response(schema: str, description: str) -> dict[str, Any]:
        error_response = {
            "description": "Protocol error",
            "content": {"application/json": {"schema": ref("ProtocolError")}},
        }
        return {
            "400": error_response,
            "401": error_response,
            "403": error_response,
            "404": error_response,
            "409": error_response,
            "413": error_response,
            "415": error_response,
            "500": error_response,
            "200": {
                "description": description,
                "content": {"application/json": {"schema": ref(schema)}},
            },
            "default": {
                "description": "Protocol error",
                "content": {"application/json": {"schema": ref("ProtocolError")}},
            },
        }

    def body(schema: str) -> dict[str, Any]:
        return {"required": True, "content": {"application/json": {"schema": ref(schema)}}}

    def post(schema: str, result: str, operation_id: str) -> dict[str, Any]:
        return {
            "post": {
                "operationId": operation_id,
                "summary": operation_id,
                "security": [{"bearerAuth": []}],
                "requestBody": body(schema),
                "responses": response(result, operation_id),
            }
        }

    paths = {
        "/health": {
            "get": {
                "operationId": "getHealth",
                "summary": "Get process health",
                "security": [],
                "responses": response("HealthReply", "Health"),
            }
        },
        "/v1/openapi.json": {
            "get": {
                "operationId": "getOpenApiDocument",
                "summary": "Get the OpenAPI contract",
                "security": [{"bearerAuth": []}],
                "responses": response("OpenApiDocument", "OpenAPI document"),
            }
        },
        "/v1/capabilities": {
            "get": {
                "operationId": "getCapabilities",
                "summary": "Get sidecar capabilities",
                "security": [{"bearerAuth": []}],
                "responses": response("CapabilitiesReply", "Capabilities"),
            }
        },
        "/v1/identities/derive": post(
            "DeriveIdentityRequest", "DeriveIdentityReply", "deriveEffectIdentity"
        ),
        "/v1/effects/claim": post("ClaimEffectRequest", "ClaimEffectReply", "claimEffect"),
    }
    for action, schema, operation_id in (
        ("renew", "RenewLeaseRequest", "renewEffectLease"),
        ("boundary", "RecordBoundaryRequest", "recordEffectBoundary"),
        ("provider-reference", "AttachProviderReferenceRequest", "attachProviderReference"),
        ("reconcile", "ReconcileEffectRequest", "reconcileEffect"),
        ("complete", "CompleteEffectRequest", "completeEffect"),
        ("fail", "FailEffectRequest", "failEffect"),
    ):
        item = post(
            schema,
            "EffectInspectionReply" if action != "reconcile" else "ReconcileEffectReply",
            operation_id,
        )
        item["post"]["parameters"] = [
            {"name": "effect_id", "in": "path", "required": True, "schema": ref("EffectId")}
        ]
        item["post"]["responses"] = response(
            {
                "renew": "RenewLeaseReply",
                "boundary": "RecordBoundaryReply",
                "provider-reference": "AttachProviderReferenceReply",
                "reconcile": "ReconcileEffectReply",
                "complete": "CompleteEffectReply",
                "fail": "FailEffectReply",
            }[action],
            operation_id,
        )
        paths[f"/v1/effects/{{effect_id}}/{action}"] = item
    paths["/v1/effects/{effect_id}"] = {
        "get": {
            "operationId": "getEffect",
            "summary": "Inspect an effect",
            "security": [{"bearerAuth": []}],
            "parameters": [
                {"name": "effect_id", "in": "path", "required": True, "schema": ref("EffectId")}
            ],
            "responses": response("EffectInspectionReply", "Effect inspection"),
        }
    }
    schemas["ReconcileEffectReply"] = {
        "allOf": [
            ref("EffectInspectionReply"),
            {
                "type": "object",
                "required": ["reconciliation"],
                "properties": {"reconciliation": {"const": "authoritative-engine-result"}},
            },
        ]
    }
    effect_example = "mycelium:effect:v1:" + "0" * 64
    schemas["DecimalValue"]["examples"] = [
        {"$type": "decimal", "profile": "decimal-1", "value": "1500.25"}
    ]
    schemas["UrlValue"]["examples"] = [
        {"$type": "url", "profile": "url-1", "value": "https://example.com/resource"}
    ]
    schemas["HealthReply"]["examples"] = [{"status": "ok", "protocol_version": PROTOCOL_VERSION}]
    schemas["CapabilitiesReply"]["examples"] = [
        {
            "protocol_version": PROTOCOL_VERSION,
            "identity_namespace": "identity-v1",
            "capabilities": ["effects", "identity", "capabilities"],
            "operations": ["claim_effect", "complete_effect"],
            "development_only": True,
        }
    ]
    identity_example = {
        "application_id": "app-a",
        "business_request_id": "request-123",
        "canonicalization_version": CANONICALIZATION_VERSION,
        "destination": {"resource_id": "resource-42"},
        "execution_scope": {"environment": "development"},
        "identity_version": IDENTITY_VERSION,
        "input": {"status": "active"},
        "tenant_id": "tenant-a",
        "tool_contract_version": "1",
        "tool_id": "resource.update",
    }
    schemas["DeriveIdentityRequest"]["examples"] = [identity_example]
    schemas["DeriveIdentityReply"]["examples"] = [
        {
            "protocol_version": PROTOCOL_VERSION,
            "effect_id": effect_example,
            "canonical_json": "{}",
            "canonical_bytes": 2,
            "identity_namespace": "identity-v1",
        }
    ]
    schemas["ClaimEffectRequest"]["examples"] = [
        {**identity_example, "decision": {"allowed": True, "verdicts": [], "denied_reasons": []}}
    ]
    for name, extra in (
        ("RenewLeaseRequest", {"owner_id": "sidecar-owner", "fence": 1}),
        (
            "RecordBoundaryRequest",
            {"owner_id": "sidecar-owner", "fence": 1, "boundary": "maybe_crossed"},
        ),
        (
            "AttachProviderReferenceRequest",
            {"owner_id": "sidecar-owner", "fence": 1, "provider_operation_ref": "provider-op-42"},
        ),
        (
            "CompleteEffectRequest",
            {"owner_id": "sidecar-owner", "fence": 1, "result": {"ok": True}},
        ),
        (
            "FailEffectRequest",
            {"owner_id": "sidecar-owner", "fence": 1, "boundary": "maybe_crossed"},
        ),
        ("ReconcileEffectRequest", {}),
    ):
        schemas[name]["examples"] = [{**identity_example, **extra}]
    projection_example = {
        "protocol_version": PROTOCOL_VERSION,
        "effect_id": effect_example,
        "effect_state": "ATTEMPTING",
        "terminal_outcome": "in_flight",
        "owner_id": "sidecar-owner",
        "lease": {"leased_until": 123.0, "last_heartbeat_at": 122.0},
        "fence": 1,
        "provider_boundary": "not_crossed",
        "provider_operation_ref": None,
        "result": None,
        "decision": {"allowed": True, "verdicts": [], "denied_reasons": []},
        "error": None,
    }
    for name, disposition in (
        ("ExecuteDisposition", "EXECUTE"),
        ("StoredResultDisposition", "RETURN_STORED_RESULT"),
        ("WaitForOwnerDisposition", "WAIT_FOR_OWNER"),
        ("RecordDecisionDisposition", "RECORD_DECISION"),
        ("UnknownDisposition", "UNKNOWN"),
        ("DeniedDisposition", "DENIED"),
        ("TerminalAbortedDisposition", "TERMINAL_ABORTED"),
    ):
        schemas[name]["examples"] = [{**projection_example, "disposition": disposition}]
    schemas["ProtocolError"]["examples"] = [
        {
            "protocol_version": PROTOCOL_VERSION,
            "error": {
                "code": "STALE_FENCE",
                "message": "ledger rejected the fenced operation",
                "retryable": False,
                "caller_action_required": True,
                "state_may_have_changed": True,
                "effect_may_have_happened": True,
                "effect_id": effect_example,
            },
        },
        {
            "protocol_version": PROTOCOL_VERSION,
            "error": {
                "code": "AUTHENTICATION_REQUIRED",
                "message": "bearer token required",
                "retryable": False,
                "caller_action_required": True,
                "state_may_have_changed": False,
                "effect_may_have_happened": False,
            },
        },
        {
            "protocol_version": PROTOCOL_VERSION,
            "error": {
                "code": "RECONCILIATION_UNAVAILABLE",
                "message": "reconciliation could not resolve the effect",
                "retryable": False,
                "caller_action_required": True,
                "state_may_have_changed": True,
                "effect_may_have_happened": True,
                "effect_id": effect_example,
            },
        },
    ]
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Mycelium Development Sidecar",
            "version": PROTOCOL_VERSION,
            "license": {
                "name": "Mycelium development-only protocol contract",
                "identifier": "LicenseRef-Mycelium-Development-Only",
            },
            "description": (
                f"Frozen development protocol {PROTOCOL_VERSION} over loopback JSON. "
                "Python remains authoritative. No exactly-once or hostile-client guarantee."
            ),
        },
        "servers": [{"url": "http://127.0.0.1"}],
        "security": [{"bearerAuth": []}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Local development bearer token in Authorization only.",
                }
            },
            "schemas": {
                **schemas,
                "OpenApiDocument": {
                    "type": "object",
                    "required": ["openapi", "info", "paths"],
                    "properties": {
                        "openapi": {"const": "3.1.0"},
                        "info": {"type": "object"},
                        "paths": {"type": "object"},
                    },
                    "additionalProperties": True,
                },
            },
        },
        "x-development-only": True,
        "x-protocol-revision": PROTOCOL_VERSION,
        "x-protocol-status": "frozen-development-alpha",
        "x-authentication": (
            "All /v1 routes require Authorization: Bearer. "
            "/health is the only unauthenticated route."
        ),
        "x-error-http-status": {
            "400": "Malformed or invalid request",
            "401": "Authentication required or invalid",
            "403": "Tenant, application, or policy denial",
            "404": "Effect not found",
            "409": "Identity, ownership, fence, or transition conflict",
            "413": "Request or response too large",
            "415": "Unsupported content type",
            "500": "Internal protocol error",
        },
        "x-stable-error-codes": sorted(
            schemas["ProtocolError"]["properties"]["error"]["properties"]["code"]["enum"]
        ),
        "paths": paths,
    }


class SidecarError(ValueError):
    """Safe protocol error with a stable code and HTTP status."""

    def __init__(self, code: str, message: str, *, status: int = 400, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


def _effect_path_segment(value: str) -> str:
    try:
        decoded = urllib.parse.unquote(value, errors="strict")
    except UnicodeDecodeError as exc:
        raise SidecarError("INVALID_REQUEST", "effect ID path is invalid") from exc
    if "/" in decoded or "\\" in decoded:
        raise SidecarError("INVALID_REQUEST", "effect ID path is invalid")
    return decoded


def _validate_structure(value: Any, *, depth: int = 0) -> None:
    if depth > 100:
        raise SidecarError("UNSUPPORTED_VALUE_TYPE", "JSON nesting is too deep")
    if isinstance(value, list):
        if len(value) > 10000:
            raise SidecarError("UNSUPPORTED_VALUE_TYPE", "JSON array is too large")
        for item in value:
            _validate_structure(item, depth=depth + 1)
    elif isinstance(value, dict):
        if len(value) > 1000:
            raise SidecarError("UNSUPPORTED_VALUE_TYPE", "JSON object is too large")
        for key, item in value.items():
            if len(key.encode("utf-8", "strict")) > 1024:
                raise SidecarError("UNSUPPORTED_VALUE_TYPE", "JSON key is too large")
            _validate_structure(item, depth=depth + 1)


def _validate_unicode(value: Any) -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SidecarError("INVALID_UNICODE", "string is not valid Unicode") from exc
    elif isinstance(value, list):
        for item in value:
            _validate_unicode(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_unicode(key)
            _validate_unicode(item)


def _jcs(value: Any) -> str:
    """Serialize the jcs-1 JSON value subset without language fallbacks."""
    _validate_unicode(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SidecarError("NON_FINITE_NUMBER", "non-finite numbers are not supported")
        raise SidecarError("NON_CANONICAL_NUMBER", "floating-point values are not supported")
    if isinstance(value, int) and not isinstance(value, bool):
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise SidecarError("INTEGER_OUT_OF_RANGE", "integer is outside the safe range")
    if isinstance(value, dict):
        ordered = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(_jcs(key) + ":" + _jcs(value[key]) for key in ordered) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_jcs(item) for item in value) + "]"
    if value is None or isinstance(value, (bool, int, str)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    raise SidecarError("UNSUPPORTED_VALUE_TYPE", "identity contains an unsupported value")


def derive_identity_v1(preimage: dict[str, Any]) -> tuple[str, str]:
    """Return canonical identity JSON and the engine-derived identity-v1 ID."""
    fields = {
        "application_id",
        "business_request_id",
        "canonicalization_version",
        "destination",
        "execution_scope",
        "identity_version",
        "input",
        "tenant_id",
        "tool_contract_version",
        "tool_id",
    }
    if set(preimage) != fields:
        raise SidecarError("IDENTITY_REQUIRED", "identity-v1 requires its exact preimage fields")
    for key in fields - {"destination", "execution_scope", "input"}:
        if (
            not isinstance(preimage[key], str)
            or not preimage[key].strip()
            or len(preimage[key].encode()) > 1024
        ):
            raise SidecarError("IDENTITY_REQUIRED", f"{key} must be a non-empty identifier")
    if preimage["identity_version"] != IDENTITY_VERSION:
        raise SidecarError("UNSUPPORTED_IDENTITY_VERSION", "only identity version 1 is supported")
    if preimage["canonicalization_version"] != CANONICALIZATION_VERSION:
        raise SidecarError("UNSUPPORTED_CANONICALIZATION_VERSION", "only jcs-1 is supported")
    if not isinstance(preimage["execution_scope"], dict):
        raise SidecarError("IDENTITY_REQUIRED", "execution_scope must be an object")
    if preimage["destination"] is not None and not isinstance(preimage["destination"], dict):
        raise SidecarError("IDENTITY_REQUIRED", "destination must be an object or null")
    _validate_structure(preimage)
    canonical = _jcs(preimage)
    encoded = canonical.encode("utf-8")
    if len(encoded) > MAX_PREIMAGE_BYTES:
        raise SidecarError("IDENTITY_PREIMAGE_TOO_LARGE", "canonical identity exceeds 64 KiB")
    digest = hashlib.sha256(b"mycelium.effect.v1\n" + encoded).hexdigest()
    return canonical, f"mycelium:effect:v1:{digest}"


def _validate_typed_values(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _validate_typed_values(item)
    elif isinstance(value, dict):
        marker = value.get("$type")
        if marker in {"decimal", "url"}:
            profile = value.get("profile")
            if profile != f"{marker}-1":
                raise SidecarError("INVALID_" + marker.upper(), f"unsupported {marker} profile")
            if set(value) - {"$type", "profile", "value"} or not isinstance(
                value.get("value"), str
            ):
                raise SidecarError("INVALID_" + marker.upper(), f"invalid {marker} value")
            text = value["value"]
            if marker == "decimal":
                import re

                if len(text) > 40 or not re.fullmatch(
                    r"(?:(?:0|[1-9][0-9]*|-[1-9][0-9]*)(?:\.[0-9]{0,17}[1-9])?|-0\.[0-9]{0,17}[1-9])",
                    text,
                ):
                    raise SidecarError("NON_CANONICAL_DECIMAL", "decimal is not in decimal-1 form")
                if len(text.replace("-", "").replace(".", "")) > 38:
                    raise SidecarError("NON_CANONICAL_DECIMAL", "decimal exceeds 38 digits")
            else:
                _validate_url(text)
        for item in value.values():
            _validate_typed_values(item)


def _validate_url(value: str) -> None:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SidecarError("INVALID_URL", "URL contains controls or is empty")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise SidecarError("INVALID_URL", "URL syntax is invalid") from exc
    scheme_end = value.find(":")
    if scheme_end <= 0 or value[:scheme_end] != value[:scheme_end].lower():
        raise SidecarError("INVALID_URL", "URL scheme must be lowercase")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
        raise SidecarError("INVALID_URL", "URL must be an absolute HTTP(S) URL without fragment")
    if parsed.username is not None or parsed.password is not None:
        raise SidecarError("INVALID_URL", "URL credentials are prohibited")
    try:
        host = parsed.hostname
        if not host or any(ord(char) > 127 for char in host):
            raise ValueError
        parsed.port
    except ValueError as exc:
        raise SidecarError("INVALID_URL", "URL host or port is invalid") from exc
    netloc_end = len(value)
    for delimiter in "/?#":
        position = value.find(delimiter, scheme_end + 3)
        if position >= 0:
            netloc_end = min(netloc_end, position)
    raw_netloc = value[scheme_end + 3 : netloc_end]
    raw_host = raw_netloc.rsplit("@", 1)[-1]
    if raw_host.startswith("["):
        raw_host = raw_host[1:].split("]", 1)[0]
    elif raw_host.count(":") == 1:
        raw_host = raw_host.rsplit(":", 1)[0]
    if raw_host != raw_host.lower():
        raise SidecarError("INVALID_URL", "URL scheme and DNS host must be lowercase")
    normalized = value.replace(parsed.scheme, parsed.scheme.lower(), 1)
    if parsed.scheme != parsed.scheme.lower():
        raise SidecarError("INVALID_URL", "URL scheme and DNS host must be lowercase")
    if normalized != value:
        raise SidecarError("INVALID_URL", "URL is not in url-1 form")


def _read_token_file(path: str | os.PathLike[str]) -> str:
    token_path = Path(path)
    try:
        info = token_path.lstat()
    except OSError as exc:
        raise ValueError("cannot read sidecar token file") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("sidecar token file must be a regular owner-only file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("sidecar token file must be owned by the current user")
    try:
        raw = token_path.read_bytes()
        token = raw.decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("sidecar token file must contain ASCII") from exc
    if token.endswith("\n"):
        token = token[:-1]
        if token.endswith("\r"):
            token = token[:-1]
    if (
        not token
        or any(char.isspace() for char in token)
        or not re.fullmatch(r"(?:[A-Za-z0-9_-]{43}|[0-9a-fA-F]{64})", token)
    ):
        raise ValueError("sidecar token must be 43 base64url or 64 hexadecimal characters")
    return token


@dataclass(frozen=True)
class SidecarConfig:
    host: str
    port: int
    tenant_id: str
    application_id: str
    token: str
    ledger_path: Path | None = None
    outcome_path: Path | None = None
    protocol_version: str = PROTOCOL_VERSION
    identity_namespace: str = "identity-v1"
    body_limit: int = MAX_BODY_BYTES
    legacy_inspection: bool = False
    profile: str = "development"
    ledger_type: str = "file"
    ledger_dsn: str | None = None
    ledger_table: str = "mycelium_action_ledger"
    outcome_type: str = "file"
    outcome_dsn: str | None = None
    outcome_table: str = "mycelium_outcomes"
    bearer_tokens: tuple[str, ...] = ()
    db_connect_timeout: float = 5.0
    db_pool_timeout: float = 5.0
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    request_timeout: float = 10.0

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as exc:
            raise ValueError("sidecar host must be a literal loopback address") from exc
        if self.profile not in {"development", "shared"}:
            raise ValueError("sidecar profile must be development or shared")
        if self.profile == "development" and not address.is_loopback:
            raise ValueError("development sidecar host must resolve exclusively to loopback")
        if not self.tenant_id or not self.application_id:
            raise ValueError("tenant_id and application_id are required")
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"only protocol version {PROTOCOL_VERSION} is supported")
        if self.identity_namespace != "identity-v1":
            raise ValueError("only identity-v1 namespace is supported")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if not 1 <= self.body_limit <= 16 * MAX_BODY_BYTES:
            raise ValueError("body_limit is outside the development limit")
        tokens = self.bearer_tokens or (self.token,)
        if not tokens or any(
            not isinstance(value, str)
            or not re.fullmatch(r"(?:[A-Za-z0-9_-]{43}|[0-9a-fA-F]{64})", value)
            for value in tokens
        ):
            raise ValueError("tokens must be 43 base64url or 64 hexadecimal characters")
        object.__setattr__(self, "bearer_tokens", tuple(tokens))
        if self.ledger_type == "postgres":
            if not self.ledger_dsn or self.profile != "shared":
                raise ValueError("shared Postgres ledger requires a database URL")
        elif self.ledger_type != "file" or self.ledger_path is None:
            raise ValueError("ledger must be file with an absolute path or postgres with a URL")
        if self.profile == "shared" and self.outcome_type != "postgres":
            raise ValueError("shared profile requires Postgres outcome storage")
        if self.outcome_type == "postgres" and not self.outcome_dsn:
            raise ValueError("Postgres outcome storage requires a database URL")
        if self.request_timeout <= 0 or self.request_timeout > 120:
            raise ValueError("request_timeout must be between 0 and 120 seconds")

    @classmethod
    def from_yaml(cls, path: str | os.PathLike[str]) -> SidecarConfig:
        import yaml

        config_path = Path(path)
        if not config_path.is_absolute():
            raise ValueError("sidecar config path must be absolute")
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError("invalid sidecar configuration") from exc
        if not isinstance(data, dict) or data.get("kind") != "mycelium-sidecar":
            raise ValueError("sidecar config kind must be mycelium-sidecar")
        token_files = data.get("bearer_token_files", [data.get("bearer_token_file")])
        if not isinstance(token_files, list) or not token_files or any(
            not isinstance(item, str) or not Path(item).is_absolute() for item in token_files
        ):
            raise ValueError("bearer_token_files must contain absolute paths")
        ledger = data.get("ledger")
        outcome = data.get("outcome_storage")
        if (
            not isinstance(ledger, dict)
            or ledger.get("type") not in {"file", "postgres"}
        ):
            raise ValueError("a file ledger path is required for the prototype")
        if (
            not isinstance(outcome, dict)
            or outcome.get("type") not in {"file", "postgres"}
        ):
            raise ValueError("a file outcome path is required for the prototype")
        if ledger.get("type") == "file" and (
            not isinstance(ledger.get("path"), str) or not Path(ledger["path"]).is_absolute()
        ):
            raise ValueError("file ledger path must be absolute")
        if outcome.get("type") == "file" and (
            not isinstance(outcome.get("path"), str) or not Path(outcome["path"]).is_absolute()
        ):
            raise ValueError("file outcome path must be absolute")
        values = data.get("server", {})
        if not isinstance(values, dict):
            raise ValueError("server configuration must be an object")
        database = data.get("database", {})
        if not isinstance(database, dict):
            raise ValueError("database configuration must be an object")
        database_url = ledger.get("url") or ledger.get("dsn") or os.environ.get(
            ledger.get("url_env", "")
        )
        outcome_url = outcome.get("url") or outcome.get("dsn") or os.environ.get(
            outcome.get("url_env", "")
        )
        return cls(
            host=values.get("host", "127.0.0.1"),
            port=int(values.get("port", 0)),
            tenant_id=data.get("tenant_id", ""),
            application_id=data.get("application_id", ""),
            token=_read_token_file(token_files[0]),
            ledger_path=Path(ledger["path"]) if ledger.get("path") else None,
            outcome_path=Path(outcome["path"]) if outcome.get("path") else None,
            protocol_version=data.get("protocol_version", PROTOCOL_VERSION),
            identity_namespace=data.get("identity_namespace", ""),
            body_limit=int(data.get("request_body_limit", MAX_BODY_BYTES)),
            legacy_inspection=bool(data.get("legacy_inspection", False)),
            profile=str(data.get("profile", "development")),
            ledger_type=str(ledger["type"]),
            ledger_dsn=database_url,
            ledger_table=str(ledger.get("table", "mycelium_action_ledger")),
            outcome_type=str(outcome["type"]),
            outcome_dsn=outcome_url,
            outcome_table=str(outcome.get("table", "mycelium_outcomes")),
            bearer_tokens=tuple(_read_token_file(item) for item in token_files),
            db_connect_timeout=float(database.get("connect_timeout", 5.0)),
            db_pool_timeout=float(database.get("pool_timeout", 5.0)),
            db_pool_min_size=int(database.get("pool_min_size", 1)),
            db_pool_max_size=int(database.get("pool_max_size", 10)),
            request_timeout=float(values.get("request_timeout", 10.0)),
        )


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    application_id: str
    capabilities: frozenset[str]
    principal_type: str = "development-token"
    audit_id: str = "sidecar-local"


def _identity_body(body: dict[str, Any], principal: Principal) -> dict[str, Any]:
    for key, expected in (
        ("tenant_id", principal.tenant_id),
        ("application_id", principal.application_id),
    ):
        if body.get(key) != expected:
            raise SidecarError(
                "TENANT_MISMATCH" if key == "tenant_id" else "APPLICATION_MISMATCH",
                f"{key} is not bound to this sidecar",
                status=403,
            )
    required = (
        "application_id",
        "business_request_id",
        "canonicalization_version",
        "destination",
        "execution_scope",
        "identity_version",
        "input",
        "tenant_id",
        "tool_contract_version",
        "tool_id",
    )
    missing = [key for key in required if key not in body]
    if missing:
        raise SidecarError("IDENTITY_REQUIRED", "missing identity fields: " + ",".join(missing))
    _validate_typed_values(body["input"])
    return {key: body[key] for key in required}


def _projection(entry: Any) -> dict[str, Any]:
    data = entry.to_dict()
    return {
        "effect_id": data.get("effect_id"),
        "effect_state": entry.resolved_effect_state().value,
        "terminal_outcome": data.get("terminal_outcome"),
        "owner_id": data.get("owner"),
        "lease": {
            "leased_until": data.get("lease_until"),
            "last_heartbeat_at": data.get("last_heartbeat_at"),
        },
        "fence": data.get("fence"),
        "provider_boundary": data.get("side_effect_boundary"),
        "provider_operation_ref": data.get("external_operation_ref"),
        "result": data.get("result"),
        "decision": data.get("decision"),
        "error": data.get("error"),
    }


class SidecarService:
    """Thin protocol adapter over one existing ActionLedger."""

    def __init__(
        self, ledger: ActionLedger, config: SidecarConfig, outcome: OutcomeEmitter | None = None
    ):
        self.ledger, self.config = ledger, config
        self._claim_lock = threading.Lock() if config.profile == "development" else None
        self.principal = Principal(
            config.tenant_id,
            config.application_id,
            frozenset({"effects", "identity", "capabilities"}),
        )
        self.outcome = outcome

    def _emit(self, entry: Any, event: str) -> None:
        if self.outcome is None:
            return
        try:
            self.outcome.emit_event(
                tool=entry.tool,
                request_id=entry.request_id,
                event=event,
                terminal_outcome=entry.terminal_outcome,
                side_effect_boundary=entry.side_effect_boundary,
                owner=entry.owner,
            )
        except Exception:
            return

    def _identity(self, body: dict[str, Any]) -> tuple[str, str]:
        canonical, effect_id = derive_identity_v1(_identity_body(body, self.principal))
        expected = body.get("expected_effect_id", body.get("effect_id"))
        if expected is not None and expected != effect_id:
            raise SidecarError(
                "EFFECT_ID_MISMATCH", "expected effect ID does not match engine derivation"
            )
        return canonical, effect_id

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "protocol_version": self.config.protocol_version}

    def ready(self) -> dict[str, Any]:
        validator = getattr(self.ledger._storage, "validate", None)
        if validator is not None:
            validator()
        return {"status": "ready", "protocol_version": self.config.protocol_version}

    def capabilities(self) -> dict[str, Any]:
        return {
            "protocol_version": self.config.protocol_version,
            "identity_namespace": self.config.identity_namespace,
            "capabilities": sorted(self.principal.capabilities),
            "operations": [
                "derive_identity",
                "claim_effect",
                "inspect_effect",
                "renew_lease",
                "record_boundary",
                "attach_provider_reference",
                "reconcile",
                "complete_effect",
                "fail_effect",
                "request_reconciliation",
            ],
            "development_only": True,
        }

    def derive(self, body: dict[str, Any]) -> dict[str, Any]:
        canonical, effect_id = self._identity(body)
        return {
            "protocol_version": self.config.protocol_version,
            "effect_id": effect_id,
            "canonical_json": canonical,
            "canonical_bytes": len(canonical.encode()),
            "identity_namespace": self.config.identity_namespace,
        }

    @staticmethod
    def _binding(body: dict[str, Any]) -> ToolTransitionBinding:
        return ToolTransitionBinding.for_tool(
            agent_id="sidecar",
            policy_version="sidecar-development",
            side_effect_class=SideEffectClass.IRREVERSIBLE,
            retry_permission=RetryPermission.MANUAL_RECONCILIATION_REQUIRED,
            capability=ToolCapability.BLIND,
        )

    def claim(self, body: dict[str, Any]) -> dict[str, Any]:
        # The development profile runs one sidecar process. Serialize its
        # non-blocking claims so a losing request can observe the winner
        # without entering ActionLedger's polling timeout path and mutating the
        # active transition to UNKNOWN.
        if self._claim_lock is None:
            return self._claim_serialized(body)
        with self._claim_lock:
            return self._claim_serialized(body)

    def _claim_serialized(self, body: dict[str, Any]) -> dict[str, Any]:
        _, effect_id = self._identity(body)
        decision_raw = body.get("decision")
        if decision_raw is not None:
            try:
                decision = Decision.from_dict(decision_raw)
            except (TypeError, ValueError) as exc:
                raise SidecarError("INVALID_REQUEST", "invalid decision evidence") from exc
        else:
            decision = None
        existing = self.ledger.get(effect_id)
        if existing is not None:
            active = _projection(existing)
            if existing.resolved_terminal_outcome().value == "IN_FLIGHT":
                if active["effect_state"] == "INTENDED":
                    if decision is None:
                        disposition = "RECORD_DECISION"
                    else:
                        try:
                            existing = self.ledger.record_decision(
                                effect_id,
                                decision.to_dict(),
                                expected_owner=existing.owner,
                                expected_fence=existing.fence,
                            )
                        except Exception as exc:
                            raise SidecarError(
                                "INVALID_TRANSITION",
                                "decision was not authorized",
                                status=409,
                            ) from exc
                        active = _projection(existing)
                        disposition = "EXECUTE" if decision.allowed else "DENIED"
                elif active["effect_state"] == "ATTEMPTING":
                    disposition = "WAIT_FOR_OWNER"
                else:
                    disposition = None
            else:
                disposition = None
            if disposition is not None:
                self._emit(existing, "sidecar_claim")
                return {
                    "protocol_version": self.config.protocol_version,
                    "disposition": disposition,
                    **active,
                }
        try:
            tool = str(body["tool_id"])
            claim_kwargs = {
                "canonical_input": body["input"],
                "business_request_id": body["business_request_id"],
                "tenant_id": self.config.tenant_id,
                "application_id": self.config.application_id,
            }
            binding = self._binding(body)
            if self.config.profile == "shared" and existing is None:
                # The first shared-sidecar race must observe the storage CAS
                # directly. ActionLedger's normal non-blocking poll path parks
                # a loser as UNKNOWN when poll_timeout=0, which is conservative
                # for a caller but wrong for this protocol's WAIT_FOR_OWNER
                # disposition. The authoritative claim and all later mutations
                # still use ActionLedger's storage boundary and state model.
                candidate = self.ledger._new_inflight_entry(
                    effect_id,
                    tool,
                    (),
                    claim_kwargs,
                    binding=binding,
                    _effect_id=effect_id,
                )
                outcome, peer = self.ledger._try_claim_inflight(
                    candidate,
                    lease_ttl=body.get("lease_ttl") or 3600.0,
                )
                if outcome == "in_flight":
                    current = peer or self.ledger.get(effect_id)
                    if current is None:
                        raise SidecarError(
                            "INTERNAL_PROTOCOL_ERROR", "shared claim result disappeared", status=500
                        )
                    self._emit(current, "sidecar_claim")
                    return {
                        "protocol_version": self.config.protocol_version,
                        "disposition": "WAIT_FOR_OWNER",
                        **_projection(current),
                    }
                entry = peer if outcome == "completed" and peer is not None else (
                    self.ledger.get(effect_id) or candidate
                )
            else:
                entry = self.ledger.claim_side_effecting(
                    effect_id,
                    tool,
                    (),
                    claim_kwargs,
                    binding,
                    lease_ttl=body.get("lease_ttl"),
                    poll_timeout=0,
                    _effect_id=effect_id,
                )
            if (
                decision is not None
                and entry.decision is None
                and entry.resolved_effect_state().value == "INTENDED"
            ):
                entry = self.ledger.record_decision(
                    effect_id,
                    decision.to_dict(),
                    expected_owner=entry.owner,
                    expected_fence=entry.fence,
                )
        except SidecarError:
            raise
        except Exception as exc:
            current = self.ledger.get(effect_id)
            if current is not None:
                blocked = _projection(current)
                disposition = {
                    "COMMITTED": "RETURN_STORED_RESULT",
                    "UNKNOWN": "UNKNOWN",
                    "ABORTED": "TERMINAL_ABORTED",
                }.get(blocked["effect_state"])
                if disposition is not None:
                    self._emit(current, "sidecar_claim")
                    return {
                        "protocol_version": self.config.protocol_version,
                        "disposition": disposition,
                        **blocked,
                    }
            code = (
                "ACTIVE_OWNER"
                if "in-flight" in str(exc).lower()
                else "POLICY_DENIED"
                if decision is not None and not decision.allowed
                else "INVALID_TRANSITION"
            )
            raise SidecarError(
                code, "claim was not authorized", status=409, retryable=code == "ACTIVE_OWNER"
            ) from exc
        result = _projection(entry)
        state = result["effect_state"]
        if state == "COMMITTED":
            disposition = "RETURN_STORED_RESULT"
        elif state == "UNKNOWN":
            disposition = "UNKNOWN"
        elif state == "ABORTED":
            disposition = "TERMINAL_ABORTED"
        elif decision is None:
            disposition = "RECORD_DECISION"
        elif not decision.allowed:
            disposition = "DENIED"
        elif state == "ATTEMPTING":
            disposition = "EXECUTE"
        else:
            disposition = "WAIT_FOR_OWNER"
        self._emit(entry, "sidecar_claim")
        return {
            "protocol_version": self.config.protocol_version,
            "disposition": disposition,
            **result,
        }

    def effect_command(
        self, operation: str, body: dict[str, Any], effect_id: str
    ) -> dict[str, Any]:
        _, derived = self._identity(body)
        if derived != effect_id:
            raise SidecarError("EFFECT_ID_MISMATCH", "path effect ID does not match identity")
        if operation == "reconcile":
            if self.ledger.get(effect_id) is None:
                raise SidecarError("NOT_FOUND", "effect not found", status=404)
            try:
                entry = self.ledger.claim_side_effecting(
                    effect_id,
                    str(body["tool_id"]),
                    (),
                    {
                        "canonical_input": body["input"],
                        "business_request_id": body["business_request_id"],
                        "tenant_id": self.config.tenant_id,
                        "application_id": self.config.application_id,
                    },
                    self._binding(body),
                    poll_timeout=0,
                    _effect_id=effect_id,
                )
            except Exception as exc:
                raise SidecarError(
                    "RECONCILIATION_UNAVAILABLE",
                    "reconciliation could not resolve the effect",
                    status=409,
                ) from exc
            self._emit(entry, "sidecar_reconciliation")
            return {
                "protocol_version": self.config.protocol_version,
                "reconciliation": "authoritative-engine-result",
                **_projection(entry),
            }
        entry = self.ledger.get(effect_id)
        if entry is None or (
            entry.kwargs.get("tenant_id") != self.config.tenant_id
            or entry.kwargs.get("application_id") != self.config.application_id
        ):
            raise SidecarError("NOT_FOUND", "effect not found", status=404)
        owner, fence = body.get("owner_id"), body.get("fence")
        if not isinstance(owner, str) or not owner or len(owner.encode()) > 1024:
            raise SidecarError("OWNER_REQUIRED", "owner_id is required", status=400)
        if isinstance(fence, bool) or not isinstance(fence, int) or fence <= 0:
            raise SidecarError("FENCE_REQUIRED", "a positive fence is required", status=400)
        try:
            if operation == "renew":
                entry = self.ledger.renew_lease(
                    effect_id,
                    expected_fence=fence,
                    _expected_owner=owner,
                    lease_ttl=body.get("lease_ttl"),
                )
            elif operation == "boundary":
                entry = self.ledger.advance_boundary(
                    effect_id,
                    SideEffectBoundary(body["boundary"]),
                    expected_owner=owner,
                    expected_fence=fence,
                )
            elif operation == "provider-reference":
                entry = self.ledger.attach_external_operation_ref(
                    effect_id,
                    str(body["provider_operation_ref"]),
                    expected_owner=owner,
                    expected_fence=fence,
                )
            elif operation == "complete":
                entry = self.ledger.complete(
                    effect_id, body.get("result"), expected_fence=fence, _expected_owner=owner
                )
            elif operation == "fail":
                boundary = body.get("boundary", "not_crossed")
                entry = self.ledger.fail(
                    effect_id,
                    RuntimeError("safe failure reported by host"),
                    failed_after_effect=boundary != "not_crossed",
                    expected_fence=fence,
                    _expected_owner=owner,
                )
            else:
                raise SidecarError("INVALID_REQUEST", "unknown effect operation")
        except SidecarError:
            raise
        except Exception as exc:
            message = str(exc).lower()
            code = (
                "STALE_FENCE"
                if "fence" in message or "owner" in message
                else "LEASE_LOST"
                if "lease" in message
                else "INVALID_TRANSITION"
            )
            raise SidecarError(code, "ledger rejected the fenced operation", status=409) from exc
        self._emit(entry, "sidecar_" + operation.replace("-", "_"))
        return {"protocol_version": self.config.protocol_version, **_projection(entry)}


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MyceliumSidecar/0"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self._service().config.request_timeout)

    def _service(self) -> SidecarService:
        return self.server.service  # type: ignore[attr-defined]

    def _auth(self, required: bool = True) -> None:
        if not required:
            return
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise SidecarError("AUTHENTICATION_REQUIRED", "bearer token required", status=401)
        supplied = header[7:]
        if not supplied or not any(
            hmac.compare_digest(supplied, token)
            for token in self._service().config.bearer_tokens
        ):
            raise SidecarError("AUTHENTICATION_INVALID", "invalid bearer token", status=401)

    def _reply(self, payload: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            raw = json.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "error": {
                        "code": "RESPONSE_TOO_LARGE",
                        "message": "response exceeds the development limit",
                        "retryable": False,
                        "caller_action_required": True,
                        "state_may_have_changed": True,
                        "effect_may_have_happened": True,
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            status = 413
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, exc: SidecarError) -> None:
        uncertain = {
            "ACTIVE_OWNER",
            "STALE_FENCE",
            "LEASE_LOST",
            "INVALID_TRANSITION",
            "RECONCILIATION_UNAVAILABLE",
            "INTERNAL_PROTOCOL_ERROR",
        }
        state_changed = exc.code in uncertain
        effect_may_have_happened = exc.code in {
            "ACTIVE_OWNER",
            "STALE_FENCE",
            "LEASE_LOST",
            "INVALID_TRANSITION",
            "RECONCILIATION_UNAVAILABLE",
            "INTERNAL_PROTOCOL_ERROR",
        }
        self._reply(
            {
                "protocol_version": PROTOCOL_VERSION,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "caller_action_required": not exc.retryable,
                    "state_may_have_changed": state_changed,
                    "effect_may_have_happened": effect_may_have_happened,
                },
            },
            exc.status,
        )

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urllib.parse.urlsplit(self.path).path
            if len(path.encode()) > MAX_PATH_BYTES:
                raise SidecarError("INVALID_REQUEST", "path is too long")
            self._auth(path not in {"/health", "/ready"})
            if path == "/health":
                self._reply(self._service().health())
                return
            if path == "/ready":
                try:
                    self._reply(self._service().ready())
                except Exception:
                    self._reply({"status": "not_ready", "protocol_version": PROTOCOL_VERSION}, 503)
                return
            if path == "/v1/capabilities":
                self._reply(self._service().capabilities())
                return
            if path == "/v1/openapi.json":
                self._reply(openapi_document())
                return
            if path.startswith("/v1/effects/"):
                effect_id = _effect_path_segment(path.rsplit("/", 1)[-1])
                if not effect_id.startswith("mycelium:effect:v1:"):
                    raise SidecarError("NOT_FOUND", "effect not found", status=404)
                entry = self._service().ledger.get(effect_id)
                if entry is None:
                    raise SidecarError("NOT_FOUND", "effect not found", status=404)
                if (
                    entry.kwargs.get("tenant_id") != self._service().config.tenant_id
                    or entry.kwargs.get("application_id") != self._service().config.application_id
                ):
                    raise SidecarError("NOT_FOUND", "effect not found", status=404)
                self._reply({"protocol_version": PROTOCOL_VERSION, **_projection(entry)})
                return
            raise SidecarError("NOT_FOUND", "endpoint not found", status=404)
        except SidecarError as exc:
            self._error(exc)
        except Exception:
            self._error(
                SidecarError("INTERNAL_PROTOCOL_ERROR", "internal protocol error", status=500)
            )

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._auth()
            if self.headers.get_content_type() != "application/json":
                raise SidecarError(
                    "INVALID_REQUEST", "Content-Type must be application/json", status=415
                )
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError as exc:
                raise SidecarError("INVALID_REQUEST", "invalid Content-Length") from exc
            limit = self._service().config.body_limit
            if length < 0 or length > limit:
                raise SidecarError(
                    "REQUEST_TOO_LARGE", "request body exceeds configured limit", status=413
                )
            raw = self.rfile.read(length)

            def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in items:
                    if key in result:
                        raise SidecarError("DUPLICATE_OBJECT_KEY", "duplicate JSON object key")
                    result[key] = value
                return result

            try:
                body = json.loads(raw, object_pairs_hook=pairs)
            except SidecarError:
                raise
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SidecarError("INVALID_JSON", "invalid JSON request") from exc
            if not isinstance(body, dict):
                raise SidecarError("INVALID_REQUEST", "JSON object required")
            _validate_structure(body)
            path = urllib.parse.urlsplit(self.path).path
            if path == "/v1/identities/derive":
                result = self._service().derive(body)
                status = 200
            elif path == "/v1/effects/claim":
                result = self._service().claim(body)
                status = 200
            elif path.startswith("/v1/effects/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise SidecarError("NOT_FOUND", "endpoint not found", status=404)
                result = self._service().effect_command(
                    parts[3], body, _effect_path_segment(parts[2])
                )
                status = 200
            else:
                raise SidecarError("NOT_FOUND", "endpoint not found", status=404)
            self._reply(result, status)
        except SidecarError as exc:
            self._error(exc)
        except Exception:
            self._error(
                SidecarError("INTERNAL_PROTOCOL_ERROR", "internal protocol error", status=500)
            )

    def log_message(self, format: str, *args: Any) -> None:
        return


class SidecarServer(http.server.ThreadingHTTPServer):
    """HTTP server that can only bind to a validated loopback address."""

    def __init__(self, service: SidecarService):
        if not service.config.host:
            raise ValueError("sidecar host is required")
        if ":" in service.config.host:
            self.address_family = socket.AF_INET6
            address = (service.config.host, service.config.port, 0, 0)
        else:
            address = (service.config.host, service.config.port)
        super().__init__(address, _Handler)
        self.service = service

    def server_close(self) -> None:
        super().server_close()
        service = getattr(self, "service", None)
        close = getattr(service.ledger._storage, "close", None) if service else None
        if close is not None:
            close()


def build_service(config: SidecarConfig) -> SidecarService:
    if config.ledger_type == "postgres":
        storage = PostgresLedgerStorage(
            config.ledger_dsn or "",
            table=config.ledger_table,
            pool_min_size=config.db_pool_min_size,
            pool_max_size=config.db_pool_max_size,
            connect_timeout=config.db_connect_timeout,
            pool_timeout=config.db_pool_timeout,
        )
        storage.validate()
    else:
        storage = FileLedgerStorage(config.ledger_path)  # type: ignore[arg-type]
    ledger = ActionLedger(storage=storage, unclassified_policy="strict")
    if config.outcome_type == "postgres":
        outcome_storage = PostgresOutcomeStorage(
            config.outcome_dsn or config.ledger_dsn or "", table=config.outcome_table
        )
        outcome_storage.validate()
    else:
        outcome_storage = FileOutcomeStorage(config.outcome_path)  # type: ignore[arg-type]
    outcome = OutcomeEmitter(config.application_id, outcome_storage)
    return SidecarService(ledger, config, outcome)


def serve_config(config: SidecarConfig) -> None:
    server = SidecarServer(build_service(config))
    print(
        f"Mycelium sidecar listening on http://{config.host}:{server.server_port} "
        f"({config.profile})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def serve_in_thread(service: SidecarService) -> tuple[SidecarServer, threading.Thread]:
    server = SidecarServer(service)
    thread = threading.Thread(target=server.serve_forever, name="mycelium-sidecar", daemon=True)
    thread.start()
    return server, thread
