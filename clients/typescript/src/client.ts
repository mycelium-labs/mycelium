import { JsonTransport } from "./transport.js";
import { MyceliumProtocolError } from "./errors.js";
import type {
  BoundaryRequest, CapabilitiesReply, ClaimEffectRequest, ClaimReply, CompleteEffectRequest,
  EffectHandle, EffectReply, FailEffectRequest, FencedRequest, HealthReply, IdentityReply,
  IdentityRequest, ProviderReferenceRequest, ReconcileRequest,
} from "./types.js";

export const PROTOCOL_VERSION = "v1alpha1" as const;

export interface MyceliumClientOptions {
  baseUrl: string;
  token?: string;
  tokenProvider?: () => string | Promise<string>;
  tenantId?: string;
  applicationId?: string;
  timeoutMs?: number;
  fetch?: typeof globalThis.fetch;
}

function wireIdentity(input: IdentityRequest, options: MyceliumClientOptions): Record<string, unknown> {
  return {
    business_request_id: input.businessRequestId, tool_id: input.toolId,
    tool_contract_version: input.toolContractVersion, destination: input.destination,
    execution_scope: input.executionScope, input: input.input,
    tenant_id: input.tenantId ?? options.tenantId,
    application_id: input.applicationId ?? options.applicationId,
    identity_version: input.identityVersion ?? "1",
    canonicalization_version: input.canonicalizationVersion ?? "jcs-1",
    ...(input.expectedEffectId === undefined ? {} : { expected_effect_id: input.expectedEffectId }),
  };
}
function projection(raw: Record<string, unknown>): EffectReply {
  if (raw.protocol_version !== PROTOCOL_VERSION || typeof raw.effect_id !== "string" || typeof raw.effect_state !== "string") throw new MyceliumProtocolError("invalid or unsupported effect response", { code: "UNSUPPORTED_PROTOCOL", httpStatus: 200 });
  if (!["INTENDED", "ATTEMPTING", "COMMITTED", "ABORTED", "UNKNOWN"].includes(raw.effect_state)) throw new MyceliumProtocolError("unsupported effect state", { code: "UNSUPPORTED_PROTOCOL", httpStatus: 200, effectId: raw.effect_id });
  if (raw.provider_boundary !== null && raw.provider_boundary !== undefined && !["not_crossed", "maybe_crossed", "crossed"].includes(String(raw.provider_boundary))) throw new MyceliumProtocolError("unsupported provider boundary", { code: "UNSUPPORTED_PROTOCOL", httpStatus: 200, effectId: raw.effect_id });
  const lease = raw.lease && typeof raw.lease === "object" ? raw.lease as Record<string, unknown> : {};
  return {
    protocolVersion: raw.protocol_version, effectId: raw.effect_id, effectState: raw.effect_state,
    terminalOutcome: typeof raw.terminal_outcome === "string" ? raw.terminal_outcome : null,
    ownerId: typeof raw.owner_id === "string" ? raw.owner_id : null,
    lease: { leasedUntil: typeof lease.leased_until === "number" ? lease.leased_until : null, lastHeartbeatAt: typeof lease.last_heartbeat_at === "number" ? lease.last_heartbeat_at : null },
    fence: typeof raw.fence === "number" ? raw.fence : null,
    providerBoundary: raw.provider_boundary as EffectReply["providerBoundary"] ?? null,
    providerOperationRef: typeof raw.provider_operation_ref === "string" ? raw.provider_operation_ref : null,
    result: raw.result as EffectReply["result"], decision: raw.decision as EffectReply["decision"], error: raw.error as string | null,
  };
}

export class MyceliumClient {
  private readonly transport: JsonTransport;
  private readonly options: MyceliumClientOptions;
  constructor(options: MyceliumClientOptions) { this.options = options; this.transport = new JsonTransport(options); }
  async health(): Promise<HealthReply> {
    const value = await this.transport.request<HealthReply>("GET", "/health", undefined, false);
    if (value.protocol_version !== PROTOCOL_VERSION || value.status !== "ok") throw new MyceliumProtocolError("unsupported sidecar health response", { code: "UNSUPPORTED_PROTOCOL", httpStatus: 200 });
    return value;
  }
  async capabilities(): Promise<CapabilitiesReply> {
    const value = await this.transport.request<CapabilitiesReply>("GET", "/v1/capabilities");
    if (value.protocol_version !== PROTOCOL_VERSION || value.identity_namespace !== "identity-v1" || value.development_only !== true || !Array.isArray(value.operations)) throw new MyceliumProtocolError("unsupported sidecar capabilities", { code: "UNSUPPORTED_PROTOCOL", httpStatus: 200 });
    return value;
  }
  async assertCompatible(): Promise<CapabilitiesReply> {
    const value = await this.capabilities();
    for (const operation of ["derive_identity", "claim_effect", "inspect_effect", "complete_effect"]) if (!value.operations.includes(operation)) throw new MyceliumProtocolError("required sidecar operation is unavailable", { code: "UNSUPPORTED_CAPABILITY", httpStatus: 200 });
    return value;
  }
  async deriveIdentity(request: IdentityRequest): Promise<IdentityReply> {
    const raw = await this.transport.request<Record<string, unknown>>("POST", "/v1/identities/derive", wireIdentity(request, this.options));
    if (raw.protocol_version !== PROTOCOL_VERSION || raw.identity_namespace !== "identity-v1" || typeof raw.effect_id !== "string" || typeof raw.canonical_json !== "string" || typeof raw.canonical_bytes !== "number") throw new MyceliumProtocolError("invalid or unsupported identity response", { code: "UNSUPPORTED_PROTOCOL", httpStatus: 200 });
    return { protocolVersion: String(raw.protocol_version), effectId: raw.effect_id, canonicalJson: raw.canonical_json, canonicalBytes: raw.canonical_bytes, identityNamespace: String(raw.identity_namespace) };
  }
  async claimEffect(request: ClaimEffectRequest): Promise<ClaimReply> {
    const raw = await this.transport.request<Record<string, unknown>>("POST", "/v1/effects/claim", { ...wireIdentity(request, this.options), ...(request.decision === undefined ? {} : { decision: request.decision }), ...(request.leaseTtl === undefined ? {} : { lease_ttl: request.leaseTtl }) });
    const effect = projection(raw);
    const disposition = raw.disposition;
    const known = ["EXECUTE", "RETURN_STORED_RESULT", "WAIT_FOR_OWNER", "RECORD_DECISION", "UNKNOWN", "DENIED", "TERMINAL_ABORTED"];
    if (typeof disposition !== "string" || !known.includes(disposition)) throw new MyceliumProtocolError("unsupported claim disposition", { code: "UNSUPPORTED_PROTOCOL", httpStatus: 200, effectId: effect.effectId });
    if (disposition === "EXECUTE" && (effect.ownerId === null || effect.fence === null)) throw new MyceliumProtocolError("execution disposition lacks lease authority", { code: "INVALID_RESPONSE", httpStatus: 200, effectId: effect.effectId });
    if (disposition === "EXECUTE") {
      return {
        ...effect,
        disposition,
        handle: { effectId: effect.effectId, ownerId: effect.ownerId, fence: effect.fence, identity: request },
      } as ClaimReply;
    }
    return { ...effect, disposition } as ClaimReply;
  }
  async getEffect(effectId: string): Promise<EffectReply> { return projection(await this.transport.request<Record<string, unknown>>("GET", `/v1/effects/${encodeURIComponent(effectId)}`)); }
  private handle(handle: EffectHandle, body: Record<string, unknown>): Record<string, unknown> { return { ...wireIdentity(handle.identity, this.options), ...body, owner_id: handle.ownerId, fence: handle.fence }; }
  async renewLease(handle: EffectHandle, request: { leaseTtl?: number } = {}): Promise<EffectReply> { return projection(await this.transport.request("POST", `/v1/effects/${encodeURIComponent(handle.effectId)}/renew`, this.handle(handle, { ...(request.leaseTtl === undefined ? {} : { lease_ttl: request.leaseTtl }) }))); }
  async recordBoundary(handle: EffectHandle, request: Omit<BoundaryRequest, keyof FencedRequest>): Promise<EffectReply> { return projection(await this.transport.request("POST", `/v1/effects/${encodeURIComponent(handle.effectId)}/boundary`, this.handle(handle, { boundary: request.boundary }))); }
  async attachProviderReference(handle: EffectHandle, request: Omit<ProviderReferenceRequest, keyof FencedRequest>): Promise<EffectReply> { return projection(await this.transport.request("POST", `/v1/effects/${encodeURIComponent(handle.effectId)}/provider-reference`, this.handle(handle, { provider_operation_ref: request.providerOperationRef }))); }
  async completeEffect(handle: EffectHandle, request: Omit<CompleteEffectRequest, keyof FencedRequest>): Promise<EffectReply> { return projection(await this.transport.request("POST", `/v1/effects/${encodeURIComponent(handle.effectId)}/complete`, this.handle(handle, { result: request.result }))); }
  async failEffect(handle: EffectHandle, request: Omit<FailEffectRequest, keyof FencedRequest> = {}): Promise<EffectReply> { return projection(await this.transport.request("POST", `/v1/effects/${encodeURIComponent(handle.effectId)}/fail`, this.handle(handle, { ...(request.boundary === undefined ? {} : { boundary: request.boundary }) }))); }
  async reconcileEffect(effectId: string, request: ReconcileRequest): Promise<EffectReply> {
    const raw = await this.transport.request<Record<string, unknown>>("POST", `/v1/effects/${encodeURIComponent(effectId)}/reconcile`, wireIdentity(request, this.options));
    const effect = projection(raw);
    if (raw.reconciliation !== "authoritative-engine-result") throw new MyceliumProtocolError("invalid reconciliation response", { code: "INVALID_RESPONSE", httpStatus: 200, effectId: effect.effectId });
    return effect;
  }
}
