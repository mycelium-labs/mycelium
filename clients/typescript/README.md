# Experimental Mycelium sidecar client

This package is the first external-language interoperability experiment for
Mycelium. The protocol remains language-neutral and the Python sidecar remains the
authority for identity, policy, claims, fencing, state transitions, and recovery.
It targets Node.js 18 or newer with native `fetch` and requires the frozen
development protocol `v1alpha1`. Version `0.1.0` is published on npm as an
experimental preview.

```sh
npm install @mycelium-labs/sidecar-client@experimental
```

## Start the sidecar

Use the Python SDK command with an absolute configuration path:

```sh
mycelium sidecar serve --config /absolute/path/sidecar.yaml
```

The configuration points to an owner-only token file. The client sends that token
only in the `Authorization` header. It never puts the token in a URL or logs it.
Browser use is unsupported because the development sidecar intentionally does not
provide permissive CORS or browser authentication.

## Use the client

```ts
import { MyceliumClient, decimal } from "@mycelium-labs/sidecar-client";

const client = new MyceliumClient({
  baseUrl: "http://127.0.0.1:8080",
  token: process.env.MYCELIUM_SIDECAR_TOKEN!,
  tenantId: "tenant-a",
  applicationId: "app-a",
});

await client.assertCompatible();
const claim = await client.claimEffect({
  businessRequestId: "request-123",
  toolId: "resource.update",
  toolContractVersion: "1",
  destination: { resourceId: "resource-42" },
  executionScope: { environment: "development" },
  input: { status: "active", amount: decimal("1500.25") },
  decision: { allowed: true, verdicts: [], denied_reasons: [] },
});

if (claim.disposition === "EXECUTE") {
  const handle = claim.handle;
  await client.recordBoundary(handle, { boundary: "maybe_crossed" });
  try {
    const result = await provider.updateResource("resource-42");
    await client.completeEffect(handle, { result });
  } catch (error) {
    await client.failEffect(handle, { boundary: "maybe_crossed" });
    throw error;
  }
} else if (claim.disposition === "RETURN_STORED_RESULT") {
  return claim.result;
} // Every other disposition means: do not call the provider.
```

The handle is only transport convenience. The sidecar revalidates ownership and
fence on every mutation. There is no local ledger, state machine, retry middleware,
or automatic provider call. A timeout or connection reset means the state may have
changed and is not proof of safe failure. Inspect the effect before deciding what to
do next. Direct provider calls outside this lifecycle bypass the protection.

The client preserves `UNKNOWN`, denial, terminal, and wait dispositions. Unknown
future dispositions fail closed. Reconciliation, provider attestation, operator
authorization, hostile clients, remote hosting, multi-tenancy, and production auth
remain outside this experiment.

## Protocol examples and OpenAPI

The sidecar exposes authenticated capabilities and OpenAPI at
`GET /v1/capabilities` and `GET /v1/openapi.json`; `/health` is the only unauthenticated
route. A language-neutral request works without this package:

```sh
curl -H "Authorization: Bearer $MYCELIUM_SIDECAR_TOKEN" \
  http://127.0.0.1:8080/v1/capabilities
```

OpenAPI 3.1 describes the routes, bearer scheme, strict request bodies, per-operation
responses, claim disposition union, typed values, and stable errors. It is suitable
for generator experiments in TypeScript, Go, Java, or Rust. Disposition unions are
runtime protocol values, so clients must reject unknown values.

## Development

```sh
npm install
npm run build
npm run typecheck
```

This package is experimental. npm attached both `experimental` and `latest` to
the first published version; attempted removal of the automatic `latest` tag was
rejected by the registry. Use the explicit `@experimental` install command and
do not treat the package as a production-supported SDK.
