import assert from "node:assert/strict";

import {
  MyceliumClient,
  MyceliumProtocolError,
  MyceliumTransportError,
} from "./dist/index.js";

const baseUrl = required("MYCELIUM_CONFORMANCE_URL");
const token = required("MYCELIUM_CONFORMANCE_TOKEN");
const tenantId = required("MYCELIUM_CONFORMANCE_TENANT");
const applicationId = required("MYCELIUM_CONFORMANCE_APPLICATION");
const expectedEffectId = required("MYCELIUM_CONFORMANCE_EFFECT_ID");

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`missing ${name}`);
  return value;
}

function identity(businessRequestId) {
  return {
    businessRequestId,
    toolId: "external_operation",
    toolContractVersion: "1",
    destination: { id: "record-9", kind: "record" },
    executionScope: { entity: "record-9", tenant: tenantId },
    input: { operation: "update", value: "new-value" },
  };
}

const decision = { allowed: true, verdicts: [], denied_reasons: [] };

async function expectError(action, ErrorType, code) {
  try {
    await action();
  } catch (error) {
    assert(error instanceof ErrorType, `expected ${ErrorType.name}, got ${error}`);
    if (code !== undefined) assert.equal(error.code, code);
    return error;
  }
  assert.fail(`expected ${ErrorType.name}`);
}

const client = new MyceliumClient({
  baseUrl,
  token,
  tenantId,
  applicationId,
});
const checks = [];

await client.assertCompatible();
checks.push("capabilities");

const derived = await client.deriveIdentity(identity("request-884"));
assert.equal(derived.effectId, expectedEffectId);
checks.push("identity-fixture");

const unauthorized = new MyceliumClient({
  baseUrl,
  token: "x".repeat(43),
  tenantId,
  applicationId,
});
await expectError(
  () => unauthorized.capabilities(),
  MyceliumProtocolError,
  "AUTHENTICATION_INVALID",
);
checks.push("authentication");

const lifecycle = identity("conformance-typescript-lifecycle");
const claim = await client.claimEffect({ ...lifecycle, decision });
assert.equal(claim.disposition, "EXECUTE");
await client.recordBoundary(claim.handle, { boundary: "maybe_crossed" });
const completed = await client.completeEffect(claim.handle, {
  result: { client: "typescript" },
});
assert.equal(completed.effectState, "COMMITTED");
const replay = await client.claimEffect({ ...lifecycle, decision });
assert.equal(replay.disposition, "RETURN_STORED_RESULT");
assert.deepEqual(replay.result, { client: "typescript" });
checks.push("claim-complete-replay");

const staleIdentity = identity("conformance-typescript-stale-fence");
const staleClaim = await client.claimEffect({ ...staleIdentity, decision });
assert.equal(staleClaim.disposition, "EXECUTE");
const staleHandle = { ...staleClaim.handle, fence: staleClaim.handle.fence + 1 };
const staleError = await expectError(
  () => client.completeEffect(staleHandle, { result: { unsafe: true } }),
  MyceliumProtocolError,
  "STALE_FENCE",
);
assert.equal(staleError.stateMayHaveChanged, true);
assert.equal(staleError.providerEffectMayHaveHappened, true);
checks.push("stale-fence");

const unknownReply = {
  protocol_version: "v1alpha1",
  effect_id: expectedEffectId,
  effect_state: "ATTEMPTING",
  terminal_outcome: "in_flight",
  owner_id: "future-owner",
  lease: { leased_until: 1, last_heartbeat_at: 1 },
  fence: 1,
  provider_boundary: "not_crossed",
  provider_operation_ref: null,
  result: null,
  decision: null,
  error: null,
  disposition: "FUTURE_DISPOSITION",
};
const unknownClient = new MyceliumClient({
  baseUrl,
  token,
  tenantId,
  applicationId,
  fetch: async () =>
    new Response(JSON.stringify(unknownReply), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
});
await expectError(
  () => unknownClient.claimEffect({ ...identity("conformance-typescript-unknown"), decision }),
  MyceliumProtocolError,
  "UNSUPPORTED_PROTOCOL",
);
checks.push("unknown-value-fail-closed");

const invalidReconciliationClient = new MyceliumClient({
  baseUrl,
  token,
  tenantId,
  applicationId,
  fetch: async () =>
    new Response(
      JSON.stringify({ ...unknownReply, disposition: undefined, reconciliation: "future-result" }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ),
});
await expectError(
  () =>
    invalidReconciliationClient.reconcileEffect(
      expectedEffectId,
      identity("conformance-typescript-reconciliation"),
    ),
  MyceliumProtocolError,
  "INVALID_RESPONSE",
);
checks.push("reconciliation-marker-fail-closed");

const timeoutClient = new MyceliumClient({
  baseUrl,
  token,
  tenantId,
  applicationId,
  timeoutMs: 10,
  fetch: async (_input, init) =>
    new Promise((_resolve, reject) => {
      const signal = init?.signal;
      const abort = () => reject(new DOMException("aborted", "AbortError"));
      if (signal?.aborted) abort();
      else signal?.addEventListener("abort", abort, { once: true });
    }),
});
const timeoutError = await expectError(
  () => timeoutClient.claimEffect({ ...identity("conformance-typescript-timeout"), decision }),
  MyceliumTransportError,
);
assert.equal(timeoutError.stateMayHaveChanged, true);
assert.equal(timeoutError.providerEffectMayHaveHappened, true);
checks.push("timeout-is-uncertain");

console.log(
  JSON.stringify({
    client: "typescript",
    effect_id: derived.effectId,
    checks,
  }),
);
