# Experimental Go client for Mycelium

This is the public Go module that lets a Go application talk to a self-hosted
Mycelium sidecar. It is a helper library, not a Mycelium server and not a
second implementation of the Mycelium engine.

Module: [`github.com/mycelium-labs/mycelium/clients/go`](https://pkg.go.dev/github.com/mycelium-labs/mycelium/clients/go)

This module is the second external-language interoperability experiment for
Mycelium. The protocol remains language-neutral. Python and `ActionLedger` remain
authoritative for identity, policy, claims, fencing, state transitions, and recovery.
The client requires the frozen development protocol `v1alpha1`. Version
`v0.1.0` is published as the first experimental Go module release. Repository
HEAD is prepared for `v0.1.1`; it is not published until the
`clients/go/v0.1.1` tag is explicitly created.

## Requirements and startup

- Go 1.22 or newer
- Standard library only
- A running local or shared-profile Python sidecar

Install the module with:

```sh
go get github.com/mycelium-labs/mycelium/clients/go@v0.1.0
```

Because the module lives in a repository subdirectory, its release tag is
`clients/go/v0.1.0` even though Go users install module version `v0.1.0`.

```sh
mycelium sidecar serve --config /absolute/path/sidecar.yaml
```

Follow the [self-hosting guide](../../sdk/docs/SELF_HOSTING.md) for complete
local and shared PostgreSQL setup. The token is supplied through the
`Authorization` header only. The client accepts credential-free HTTP(S) endpoints
without query strings or fragments and rejects redirects. Use HTTPS through a
trusted reverse proxy for remote connections. It does not use browser cookies or
browser authentication.

## Client lifecycle

```go
ctx := context.Background()
client, err := mycelium.NewClient(mycelium.ClientOptions{
    BaseURL: "http://127.0.0.1:8787",
    Token: os.Getenv("MYCELIUM_SIDECAR_TOKEN"),
    TenantID: "tenant-a", ApplicationID: "app-a",
})
if err != nil { return err }
if err := client.AssertCompatible(ctx); err != nil { return err }
claim, err := client.ClaimEffect(ctx, mycelium.ClaimEffectRequest{
    IdentityRequest: mycelium.IdentityRequest{
        BusinessRequestID: "request-123", ToolID: "resource.update",
        ToolContractVersion: "1", TenantID: "tenant-a", ApplicationID: "app-a",
        Destination: mycelium.Destination{"resource_id": "resource-42"},
        ExecutionScope: mycelium.ExecutionScope{"environment": "development"},
        Input: map[string]any{"status": "active"},
    },
    Decision: &mycelium.Decision{Allowed: true, Verdicts: []mycelium.Verdict{}, DeniedReasons: []string{}},
})
if err != nil { return err }
if claim.Disposition == mycelium.ClaimExecute {
    handle := claim.Handle
    if _, err = client.RecordEffectBoundary(ctx, handle, mycelium.BoundaryMaybeCrossed); err != nil { return err }
    result, providerErr := provider.UpdateResource(ctx, "resource-42")
    if providerErr != nil { _, _ = client.FailEffect(ctx, handle, mycelium.BoundaryMaybeCrossed); return providerErr }
    _, err = client.CompleteEffect(ctx, handle, result)
} else if claim.Disposition == mycelium.ClaimStoredResult {
    result := claim.Result
    _ = result
} // Every other disposition means the provider must not run.
```

The handle is only transport convenience. The sidecar revalidates its owner and
fence on every mutation. The Go client contains no local state machine and performs
no automatic retries. A timeout, cancellation, connection reset, or HTTP 5xx does
not prove that state or provider execution remained unchanged. Inspect the effect
before deciding what to do next. Provider calls outside this lifecycle bypass the
protection.

## Types and errors

`ClaimReply` validates all seven current dispositions and rejects unknown or missing
dispositions. Only `EXECUTE` produces a handle. `UNKNOWN`, denial, terminal abort,
record-decision, and owner-wait replies never expose execution permission.

`ProtocolError` preserves stable error codes, HTTP status, retry classification, and
uncertainty flags. `TransportError` implements `errors.As` and conservatively marks
mutation state as possibly changed. Response bodies are bounded and malformed replies
are sanitized.

`Decimal` and `URL` create the approved tagged values without normalization. They
reject noncanonical decimal forms, unsafe URLs, credentials, fragments, controls,
uppercase hosts, and unsupported schemes. The sidecar repeats all validation.

## Language-neutral contract

```text
Go client          TypeScript client          Raw HTTP client
       \                    |                       /
        v                   v                      v
             Same HTTP/OpenAPI protocol
                         |
                         v
              Authoritative Mycelium engine
```

OpenAPI is served at `GET /v1/openapi.json`. It is the protocol contract, not a Go
or TypeScript model hierarchy. Generated types do not grant trust or ownership.
Browser, public multi-tenant hosting, production IAM, hostile-client protection,
provider attestation, reconciliation authority, and exactly-once guarantees remain
unsupported.
