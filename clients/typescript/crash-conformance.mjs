import { MyceliumClient } from "./dist/index.js";

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`missing ${name}`);
  return value;
}

const startAtMs = Number(process.env.MYCELIUM_CONFORMANCE_START_AT_MS ?? "0");
const holdMs = Number(process.env.MYCELIUM_CONFORMANCE_HOLD_MS ?? "0");
const leaseTtl = Number(process.env.MYCELIUM_CONFORMANCE_LEASE_TTL ?? "1");
const boundary = process.env.MYCELIUM_CONFORMANCE_BOUNDARY;

if (![startAtMs, holdMs, leaseTtl].every(Number.isFinite) || holdMs < 0 || leaseTtl <= 0) {
  throw new Error("invalid crash-conformance timing");
}
if (startAtMs > Date.now()) {
  await new Promise((resolve) => setTimeout(resolve, startAtMs - Date.now()));
}

const tenantId = required("MYCELIUM_CONFORMANCE_TENANT");
const applicationId = required("MYCELIUM_CONFORMANCE_APPLICATION");
const request = {
  businessRequestId: required("MYCELIUM_CONFORMANCE_REQUEST_ID"),
  toolId: "external_operation",
  toolContractVersion: "1",
  destination: { id: "record-9", kind: "record" },
  executionScope: { entity: "record-9", tenant: tenantId },
  input: { operation: "update", value: "new-value" },
};
const client = new MyceliumClient({
  baseUrl: required("MYCELIUM_CONFORMANCE_URL"),
  token: required("MYCELIUM_CONFORMANCE_TOKEN"),
  tenantId,
  applicationId,
});
const claim = await client.claimEffect({
  ...request,
  leaseTtl,
  decision: { allowed: true, verdicts: [], denied_reasons: [] },
});
if (claim.disposition === "EXECUTE" && boundary) {
  await client.recordBoundary(claim.handle, { boundary });
}

console.log(
  JSON.stringify({
    client: "typescript",
    disposition: claim.disposition,
    effect_id: claim.effectId,
    effect_state: claim.effectState,
    owner_id: claim.disposition === "EXECUTE" ? claim.handle.ownerId : null,
    fence: claim.disposition === "EXECUTE" ? claim.handle.fence : null,
    result: claim.result,
  }),
);

if (claim.disposition === "EXECUTE" && holdMs > 0) {
  await new Promise((resolve) => setTimeout(resolve, holdMs));
}
