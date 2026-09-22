import {
  decimal,
  url,
  type CompleteEffectRequest,
  type IdentityRequest,
  type JsonValue,
} from "@mycelium-labs/sidecar-client";

// Compile against the package exports so the published declarations are checked.
const amount = decimal("1500.25");
const destination = url("https://example.com/orders");
const values: JsonValue[] = [amount, destination];

const identity: IdentityRequest = {
  businessRequestId: "order-1",
  toolId: "create-order",
  toolContractVersion: "1",
  destination: { endpoint: destination },
  executionScope: {},
  input: { amount, destination, values },
};

const completion: CompleteEffectRequest = {
  ownerId: "worker-1",
  fence: 1,
  result: { amount, destination },
};

const directInput: IdentityRequest = { ...identity, input: amount };
const directResult: CompleteEffectRequest = { ...completion, result: destination };

// The helper fix must not admit arbitrary non-JSON values.
// @ts-expect-error Functions are not JSON values.
const invalid: JsonValue = () => "value";

void [identity, completion, directInput, directResult, invalid];
