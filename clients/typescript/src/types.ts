export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export interface DecimalValue extends JsonObject { $type: "decimal"; profile: "decimal-1"; value: string }
export interface UrlValue extends JsonObject { $type: "url"; profile: "url-1"; value: string }

export interface IdentityRequest {
  businessRequestId: string;
  toolId: string;
  toolContractVersion: string;
  destination: JsonObject | null;
  executionScope: JsonObject;
  input: JsonValue;
  tenantId?: string;
  applicationId?: string;
  identityVersion?: string;
  canonicalizationVersion?: string;
  expectedEffectId?: string;
}

export interface DecisionEvidence {
  allowed: boolean;
  verdicts: Array<{ name: string; allowed: boolean; reason?: string | null }>;
  denied_reasons: string[];
}
export interface ClaimEffectRequest extends IdentityRequest {
  decision?: DecisionEvidence;
  leaseTtl?: number;
}
export interface FencedRequest { ownerId: string; fence: number }
export type Boundary = "not_crossed" | "maybe_crossed" | "crossed";
export interface BoundaryRequest extends FencedRequest { boundary: Boundary }
export interface ProviderReferenceRequest extends FencedRequest { providerOperationRef: string }
export interface CompleteEffectRequest extends FencedRequest { result?: JsonValue }
export interface FailEffectRequest extends FencedRequest { boundary?: Boundary }
export type ReconcileRequest = IdentityRequest;

export interface HealthReply { status: string; protocol_version: string }
export interface CapabilitiesReply {
  protocol_version: string;
  identity_namespace: string;
  capabilities: string[];
  operations: string[];
  development_only: boolean;
  [key: string]: JsonValue;
}
export interface IdentityReply {
  protocolVersion: string;
  effectId: string;
  canonicalJson: string;
  canonicalBytes: number;
  identityNamespace: string;
}
export interface Lease { leasedUntil: number | null; lastHeartbeatAt: number | null }
export interface EffectReply {
  protocolVersion: string;
  effectId: string;
  effectState: string;
  terminalOutcome: string | null;
  ownerId: string | null;
  lease: Lease;
  fence: number | null;
  providerBoundary: Boundary | null;
  providerOperationRef: string | null;
  result?: JsonValue;
  decision?: DecisionEvidence | null;
  error?: string | null;
}
export interface ExecuteDisposition extends EffectReply {
  disposition: "EXECUTE";
  ownerId: string;
  fence: number;
  handle: EffectHandle;
}
export interface StoredResultDisposition extends EffectReply { disposition: "RETURN_STORED_RESULT" }
export interface WaitForOwnerDisposition extends EffectReply { disposition: "WAIT_FOR_OWNER" | "RECORD_DECISION" }
export interface UnknownDisposition extends EffectReply { disposition: "UNKNOWN" }
export interface DeniedDisposition extends EffectReply { disposition: "DENIED" }
export interface AbortedDisposition extends EffectReply { disposition: "TERMINAL_ABORTED" }
export type ClaimReply = ExecuteDisposition | StoredResultDisposition | WaitForOwnerDisposition | UnknownDisposition | DeniedDisposition | AbortedDisposition;
export interface EffectHandle {
  effectId: string;
  ownerId: string;
  fence: number;
  /** Original identity candidate, retained only to satisfy the sidecar's request binding. */
  identity: IdentityRequest;
}
