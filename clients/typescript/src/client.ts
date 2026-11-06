import { JsonTransport } from "./transport.js";
import { MyceliumLocalValidationError, MyceliumProtocolError } from "./errors.js";
import type {
  BoundaryRequest, CapabilitiesReply, ClaimEffectRequest, ClaimReply, CompleteEffectRequest,
  ClaimCompositeRequest, CompositeHandle, CompositeReply, EffectHandle, EffectReply,
  FailEffectRequest, FencedRequest, HealthReply, IdentityReply, IdentityRequest,
  ProviderReferenceRequest, ReconcileRequest,
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

// Copy only lossless JSON data before an asynchronous token lookup can run.
function identityValue(value: unknown, path: string, ancestors = new Set<object>()): unknown {
  const invalid = (reason: string): never => { throw new MyceliumLocalValidationError(`${path}: ${reason}`); };
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || Object.is(value, -0)) invalid("identity numbers must be safe integers; use decimal() for fractions");
    return value;
  }
  if (typeof value !== "object") return invalid("expected lossless JSON data");
  if (ancestors.has(value)) return invalid("cyclic identity value");
  const array = Array.isArray(value);
  const prototype = Object.getPrototypeOf(value);
  if (array ? prototype !== Array.prototype : prototype !== Object.prototype && prototype !== null) return invalid("custom objects and serialization are unsupported");
  ancestors.add(value);
  try {
    const result: Record<string, unknown> | unknown[] = array ? [] : Object.create(null);
    const keys = Reflect.ownKeys(value);
    if (array && keys.length !== value.length + 1) invalid("sparse arrays and extra array properties are unsupported");
    for (const key of keys) {
      if (array && key === "length") continue;
      if (typeof key !== "string") return invalid("symbol properties are unsupported");
      if (array && (!/^(0|[1-9][0-9]*)$/.test(key) || Number(key) >= value.length)) invalid("extra array properties are unsupported");
      const descriptor = Object.getOwnPropertyDescriptor(value, key)!;
      if (!descriptor.enumerable || !("value" in descriptor)) invalid("non-enumerable properties and accessors are unsupported");
      Object.defineProperty(result, key, { value: identityValue(descriptor.value, `${path}[${JSON.stringify(key)}]`, ancestors), enumerable: true, writable: true, configurable: true });
    }
    return result;
  } finally { ancestors.delete(value); }
}

function wireIdentity(input: IdentityRequest, options: MyceliumClientOptions): Record<string, unknown> {
  return identityValue({
    business_request_id: input.businessRequestId, tool_id: input.toolId,
    tool_contract_version: input.toolContractVersion, destination: input.destination,
    execution_scope: input.executionScope, input: input.input,
    ...((input.tenantId ?? options.tenantId) === undefined ? {} : { tenant_id: input.tenantId ?? options.tenantId }),
    ...((input.applicationId ?? options.applicationId) === undefined ? {} : { application_id: input.applicationId ?? options.applicationId }),
    identity_version: input.identityVersion ?? "1",
    canonicalization_version: input.canonicalizationVersion ?? "jcs-1",
    ...(input.expectedEffectId === undefined ? {} : { expected_effect_id: input.expectedEffectId }),
  }, "identity") as Record<string, unknown>;
}
function projection(raw: Record<string, unknown>): EffectReply {
  if (raw.protocol_version !== PROTOCOL_VERSION || typeof raw.effect_state !== "string") throw new MyceliumProtocolError("invalid or unsupported effect response", { code: "UNSUPPORTED_PROTOCOL", httpStatus: 200 });
  if (typeof raw.effect_id !== "string" || !/^mycelium:effect:v1:[0-9a-f]{64}$/.test(raw.effect_id)) throw new MyceliumProtocolError("invalid effect identity", { code: "INVALID_RESPONSE", httpStatus: 200 });
  if (!["INTENDED", "ATTEMPTING", "COMMITTED", "ABORTED", "UNKNOWN"].includes(raw.effect_state)) throw new MyceliumProtocolError("unsupported effect state", { code: "UNSUPPORTED_PROTOCOL", httpStatus: 200, effectId: raw.effect_id });
  if (raw.provider_boundary !== null && raw.provider_boundary !== undefined && !["not_crossed", "maybe_crossed", "crossed"].includes(String(raw.provider_boundary))) throw new MyceliumProtocolError("unsupported provider boundary", { code: "UNSUPPORTED_PROTOCOL", httpStatus: 200, effectId: raw.effect_id });
  if ((raw.owner_id != null && (typeof raw.owner_id !== "string" || raw.owner_id.trim() === "")) ||
      (raw.fence != null && (typeof raw.fence !== "number" || !Number.isSafeInteger(raw.fence) || raw.fence < 1))) {
    throw new MyceliumProtocolError("invalid effect ownership data", { code: "INVALID_RESPONSE", httpStatus: 200, effectId: raw.effect_id });
  }
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

function compositeProjection(raw: Record<string, unknown>): CompositeReply {
  if (raw.protocol_version !== PROTOCOL_VERSION || raw.composite_protocol_version !== "composite-v1" || typeof raw.operation_id !== "string" ||
      typeof raw.definition !== "string" || typeof raw.manifest_digest !== "string" ||
      !/^[0-9a-f]{64}$/.test(raw.manifest_digest) ||
      (raw.status !== "RUNNING" && raw.status !== "COMPLETED") ||
      typeof raw.fence !== "number" || !Number.isSafeInteger(raw.fence) || raw.fence < 1 ||
      typeof raw.next_step !== "number" || !Number.isSafeInteger(raw.next_step) ||
      !raw.children || typeof raw.children !== "object" || Array.isArray(raw.children)) {
    throw new MyceliumProtocolError("invalid composite response", { code: "INVALID_RESPONSE", httpStatus: 200 });
  }
  return {
    protocolVersion: raw.protocol_version, compositeProtocolVersion: "composite-v1", operationId: raw.operation_id,
    definition: raw.definition, manifestDigest: raw.manifest_digest,
    status: raw.status, ownerId: typeof raw.owner_id === "string" ? raw.owner_id : null,
    fence: raw.fence, leaseUntil: typeof raw.lease_until === "number" ? raw.lease_until : null,
    nextStep: raw.next_step, children: raw.children as CompositeReply["children"],
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
  async assertCompositeCompatible(): Promise<CapabilitiesReply> {
    const value = await this.assertCompatible();
    const operations = (value.extensions as Record<string, string[]> | undefined)?.["composite-v1"];
    for (const operation of ["claim_composite", "claim_composite_step", "boundary_composite_step", "complete_composite_step", "resolve_composite_step", "finish_composite"])
      if (!Array.isArray(operations) || !operations.includes(operation)) throw new MyceliumProtocolError("composite-v1 extension is unavailable", { code: "UNSUPPORTED_CAPABILITY", httpStatus: 200 });
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

  private compositeBody(handle: CompositeHandle, extra: Record<string, unknown> = {}): Record<string, unknown> {
    return {
      tenant_id: this.options.tenantId, application_id: this.options.applicationId,
      owner_id: handle.ownerId, fence: handle.fence, ...extra,
    };
  }
  private compositePath(handle: CompositeHandle): string {
    return `/extensions/composite-v1/composites/${encodeURIComponent(handle.operationId)}`;
  }
  async claimComposite(request: ClaimCompositeRequest): Promise<{ composite: CompositeReply; handle: CompositeHandle }> {
    const raw = await this.transport.request<Record<string, unknown>>("POST", "/extensions/composite-v1/composites/claim", {
      tenant_id: this.options.tenantId, application_id: this.options.applicationId,
      operation_id: request.operationId, definition: request.definition,
      steps: request.steps.map(step => ({ step_id: step.stepId, tool_id: step.toolId })),
      ...(request.leaseTtl === undefined ? {} : { lease_ttl: request.leaseTtl }),
    });
    const composite = compositeProjection(raw);
    if (!composite.ownerId) throw new MyceliumProtocolError("composite claim lacks owner", { code: "INVALID_RESPONSE", httpStatus: 200 });
    return { composite, handle: { operationId: composite.operationId, ownerId: composite.ownerId, fence: composite.fence } };
  }
  async getComposite(operationId: string): Promise<CompositeReply> {
    return compositeProjection(await this.transport.request("GET", `/extensions/composite-v1/composites/${encodeURIComponent(operationId)}`));
  }
  async renewComposite(handle: CompositeHandle, leaseTtl?: number): Promise<CompositeReply> {
    return compositeProjection(await this.transport.request("POST", `${this.compositePath(handle)}/renew`,
      this.compositeBody(handle, leaseTtl === undefined ? {} : { lease_ttl: leaseTtl })));
  }
  async releaseComposite(handle: CompositeHandle): Promise<CompositeReply> {
    return compositeProjection(await this.transport.request("POST", `${this.compositePath(handle)}/release`, this.compositeBody(handle)));
  }
  async finishComposite(handle: CompositeHandle): Promise<CompositeReply> {
    return compositeProjection(await this.transport.request("POST", `${this.compositePath(handle)}/finish`, this.compositeBody(handle)));
  }
  async claimCompositeStep(handle: CompositeHandle, stepId: string, request: ClaimEffectRequest): Promise<ClaimReply> {
    const raw = await this.transport.request<Record<string, unknown>>("POST", `${this.compositePath(handle)}/steps/${encodeURIComponent(stepId)}/claim`,
      this.compositeBody(handle, {
        identity: wireIdentity(request, this.options),
        ...(request.decision === undefined ? {} : { decision: request.decision }),
        ...(request.leaseTtl === undefined ? {} : { lease_ttl: request.leaseTtl }),
      }));
    const effect = projection(raw);
    const disposition = raw.disposition;
    if (!["EXECUTE", "RETURN_STORED_RESULT", "WAIT_FOR_OWNER", "RECORD_DECISION", "UNKNOWN", "DENIED", "TERMINAL_ABORTED"].includes(String(disposition)))
      throw new MyceliumProtocolError("invalid composite step disposition", { code: "INVALID_RESPONSE", httpStatus: 200 });
    if (disposition === "EXECUTE") {
      if (!effect.ownerId || effect.fence === null) throw new MyceliumProtocolError("composite step lacks effect authority", { code: "INVALID_RESPONSE", httpStatus: 200 });
      return { ...effect, disposition, handle: { effectId: effect.effectId, ownerId: effect.ownerId, fence: effect.fence, identity: request } } as ClaimReply;
    }
    return { ...effect, disposition } as ClaimReply;
  }
  async boundaryCompositeStep(handle: CompositeHandle, stepId: string, effect: EffectHandle): Promise<EffectReply> {
    return projection(await this.transport.request("POST", `${this.compositePath(handle)}/steps/${encodeURIComponent(stepId)}/boundary`,
      this.compositeBody(handle, { identity: wireIdentity(effect.identity, this.options), effect_owner_id: effect.ownerId, effect_fence: effect.fence })));
  }
  async completeCompositeStep(handle: CompositeHandle, stepId: string, effect: EffectHandle, result: unknown): Promise<EffectReply> {
    return projection(await this.transport.request("POST", `${this.compositePath(handle)}/steps/${encodeURIComponent(stepId)}/complete`,
      this.compositeBody(handle, { identity: wireIdentity(effect.identity, this.options), effect_owner_id: effect.ownerId, effect_fence: effect.fence, result })));
  }
  async resolveCompositeStep(handle: CompositeHandle, stepId: string, identity: IdentityRequest): Promise<CompositeReply> {
    return compositeProjection(await this.transport.request("POST", `${this.compositePath(handle)}/steps/${encodeURIComponent(stepId)}/resolve`,
      this.compositeBody(handle, { identity: wireIdentity(identity, this.options) })));
  }
}
