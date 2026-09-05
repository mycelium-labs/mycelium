# Mycelium sidecar protocol v1alpha1

**Status:** frozen development protocol with published experimental
implementations. Not production-ready.

`v1alpha1` is the first immutable interoperability target for the Mycelium
sidecar and language clients. Freezing this revision means implementations can
depend on one exact contract while it is evaluated. It does not create a stable
`v1` compatibility promise.

## Frozen identifiers

| Contract | Frozen value | Purpose |
|---|---|---|
| Sidecar protocol | `v1alpha1` | HTTP operations, messages, dispositions, and errors |
| Identity namespace | `identity-v1` | Effect identity preimage and hash construction |
| Identity version | `1` | Identity preimage field set |
| Canonicalization profile | `jcs-1` | Cross-language canonical JSON rules |
| Decimal profile | `decimal-1` | Exact decimal wire representation |
| URL profile | `url-1` | Conservative URL wire representation |

The sidecar, OpenAPI document, TypeScript client, Go client, examples, and
fixtures must use these exact values. A client must fail closed when it receives
an unsupported safety-critical version or enum value.

## Frozen surface

`v1alpha1` freezes:

- the twelve documented HTTP routes and their operation IDs;
- loopback bearer authentication and fixed tenant/application binding;
- operation-specific request and response shapes;
- the seven claim dispositions;
- owner, lease, and fence requirements;
- explicit provider-boundary reporting;
- the stable protocol-error envelope and uncertainty fields;
- engine-derived effect identities;
- the identity, canonicalization, decimal, and URL profiles;
- the rule that the Python engine remains authoritative.

The OpenAPI document returned by `openapi_document()` and served at
`GET /v1/openapi.json` is the machine-readable transport contract. The Transition
Envelope specification and decision log define rules that OpenAPI cannot express,
including fencing, recovery authority, and fail-closed behavior.

For the freeze commit, compact UTF-8 JSON with recursively sorted object keys and
no insignificant whitespace is 48,255 bytes and has this SHA-256 fingerprint:

```text
2bc2db4101b1231a8c02c7e79c116b68d2fdce1c171714cbfc8e3c5df81a73c7
```

The fingerprint is an audit aid, not the protocol version. Any intentional
wire-visible change must select a new protocol revision rather than merely update
this fingerprint under `v1alpha1`.

## Compatibility rules

- The contents of `v1alpha1` are immutable.
- A wire-visible breaking change requires a new revision such as `v1alpha2`.
- Changing identity fields or hashing requires a new identity namespace.
- Changing canonicalization semantics requires a new canonicalization profile.
- Unknown safety-critical versions, states, boundaries, or dispositions fail
  closed.
- Non-critical extensions may be ignored only where `v1alpha1` explicitly allows
  them.
- An extension cannot silently become identity-bearing or authorize execution.
- Existing `v1alpha1` records remain labeled with their original protocol,
  identity, and canonicalization versions.
- Legacy Python identities are not reinterpreted as `identity-v1` records.

## Current implementations

| Implementation | Status |
|---|---|
| Python development sidecar | Published in `mycelium-runtime==1.38.2`; implements and advertises `v1alpha1` |
| TypeScript client | Published as `@mycelium-labs/sidecar-client@0.1.0`; requires `v1alpha1` |
| Go client | Published as module `v0.1.0`; requires `v1alpha1` |
| Raw HTTP clients | May use the same authenticated OpenAPI contract |

TypeScript and Go are interoperability examples. They do not define the protocol
and do not contain authoritative transition logic.

## Experimental publication gates

Before publishing any preview package or tag:

- [x] Freeze the protocol revision as `v1alpha1`.
- [x] Freeze `identity-v1`, `jcs-1`, `decimal-1`, and `url-1` for this revision.
- [x] Align the Python sidecar, OpenAPI document, TypeScript client, and Go client.
- [x] Preserve fail-closed handling for unknown safety-critical values.
- [x] Approve the release coordinates: `mycelium-runtime==1.38.2`,
  `@mycelium-labs/sidecar-client@0.1.0`, and Go module `v0.1.0`.
- [x] Publish `@mycelium-labs/sidecar-client@0.1.0` as a public npm preview on
  the `experimental` distribution tag.
- [x] Approve the Go submodule tag `clients/go/v0.1.0`.
- [x] Approve `mycelium-runtime==1.38.2` as the first Python package version
  containing the sidecar preview.
- [x] Add Python release notes that state the development-only limitations.
- [x] Run the Python release checklist, including CI on Python 3.10 through
  3.13, package installation, concurrency proofs, lint, and Markdown links.
- [x] Obtain explicit approval before publishing or tagging anything.

## Validation recorded on 2026-09-05

- Python: `1.38.2` wheel and source archive built; distribution-content check
  passed; the wheel contains the sidecar and CLI entry point.
- Python: the existing suite passed with 1,521 tests passed and 15 skipped;
  `ruff check mycelium tests` passed. No test files were changed.
- TypeScript: typecheck, build, and npm pack dry run passed. The package contains
  13 intended files and uses the `experimental` distribution tag.
- Go: `go vet ./...` and `go build ./...` passed for the declared module path.
- Registry collision checks before release: Python `1.38.2`, the npm package
  name, and the Go submodule tag were all absent when checked.
- GitHub CI passed on the final tagged commit for Python 3.10 through 3.13,
  package installation, concurrency proofs, lint, and Markdown links.
- The corrected `v1.38.2` tag resolves to the final documented commit, and the
  GitHub Release was recreated from that tag.
- PyPI publication completed with wheel, source archive, and digital
  attestations. The live package description contains the language-neutral
  integration documentation.
- Go module `v0.1.0` was published from the CI-verified commit and confirmed
  through the public Go module proxy.
- TypeScript `0.1.0` was published publicly to npm after interactive 2FA and
  confirmed through the unauthenticated registry endpoint. The registry exposes
  the intended `experimental` tag and also retained an automatic `latest` tag
  for this first package version; an authenticated removal attempt returned
  HTTP 400.

## Production gates

The protocol freeze does not make the sidecar production-ready. Production
support requires a separately approved effort covering repeatable conformance and
failure validation, deployment topology, durable storage, monitoring,
authentication appropriate to the deployment, reconciliation requirements, and
operational recovery.

Remote hosting, multi-tenancy, hostile-client protection, provider attestation,
automatic legacy migration, and exactly-once claims remain outside `v1alpha1`.
They are not required merely to experiment with the local trusted-client profile.

## Release decision

The Python reference engine and development sidecar are published in
`mycelium-runtime==1.38.2`, the experimental TypeScript transport client is
published as `@mycelium-labs/sidecar-client@0.1.0`, and the experimental Go
transport client is published as module `v0.1.0`. The frozen protocol remains
development-only, and publication does not expand its production guarantees.
