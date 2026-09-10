# Transition Envelope identity decisions

**Status:** RFC frozen as the experimental `v1alpha1` protocol contract. These
decisions specify the `identity-v1` wire contract. D17 records the original
trusted-loopback profile boundary; the later shared PostgreSQL profile reuses
the same frozen wire semantics for self-hosted coordination. Public
multi-tenant production deployment remains out of scope.

## D1. Authoritative effect-ID derivation

- **Status:** frozen for `v1alpha1`.
- **Decision:** The engine canonicalizes the validated identity preimage and derives
  `effect_id`. A client may send a cached value only as a consistency hint.
- **Reasoning:** A client-controlled digest could fragment identity through different
  serializers or malicious field omission.
- **Compatibility:** Deliberate change. Current Python exposes local SHA-256 helpers
  and uses its current preimage; it is not silently reinterpreted.

## D2. Frozen `identity-v1` preimage

- **Status:** frozen for `v1alpha1`.
- **Decision:** The preimage has exactly these members:

  `application_id`, `business_request_id`,
  `canonicalization_version`, `destination`, `execution_scope`,
  `identity_version`, `input`, `tenant_id`, `tool_contract_version`, `tool_id`.

- **Included:** application, tenant, business operation, registered operation
  contract, identity-relevant scope/destination, and complete typed input. The
  authenticated `agent_id` is provenance and policy input, not identity, so
  workers can retry across agent instances without fragmentation.
- **Excluded:** protocol/schema version, dispatch/run/trace/span IDs, owner, lease,
  fence, timestamps, provider key/reference/response, reconciliation verdict,
  operator identity, outcome metadata, effect boundary, recovery capability,
  effect class, and policy decision evidence.
- **Reasoning:** Delivery, execution, recovery, and policy facts must not fragment
  the logical effect. `tool_contract_version` protects against silently changing
  the meaning or safety contract of a tool.

## D3. Dispatch and run identity

- **Status:** frozen for `v1alpha1`.
- **Decision:** `dispatch_id` and `run_id` are correlation metadata, not identity
  members. A new dispatch or restarted run for the same business operation must
  converge on one wire effect.
- **Compatibility:** Deliberate change from current Python, whose transition preimage
  may include `dispatch_id` and current scope fields. Migration must use a separate
  identity namespace or explicit alias mapping; no silent reinterpretation is safe.

## D4. Business request identity

- **Status:** frozen for `v1alpha1`.
- **Decision:** `business_request_id` is a required stable host-owned identity member,
  not merely an alias or transport ID. The engine may maintain a request-to-effect
  index for drift detection, but `effect_id` remains the canonical record key.
- **Consequences:** Same request plus changed meaningful input is `ARGUMENT_DRIFT`.
  Different request IDs intentionally identify different effects even with identical
  input. Randomizing the ID on retry defeats deduplication and is a host error.

## D5. Canonicalization standard and value model

- **Status:** frozen for `v1alpha1` and fixture adoption.
- **Decision:** Use RFC 8785 JSON Canonicalization Scheme with a Mycelium profile
  named `jcs-1`.
- **Universal rules:** UTF-8 output, RFC 8785 key ordering and escaping, arrays
  preserve order, unique string object keys, explicit null, and no Unicode
  normalization at the universal layer.
- **Identity input values:** null, boolean, string, safe signed integer in
  `[-9007199254740991, 9007199254740991]`, arrays, and objects recursively using
  those values. Raw floating point, NaN, infinities, negative zero, cyclic/native
  objects, and implicit string conversions are rejected.
- **Typed representations:** exact decimals use the tagged decimal-1 object or
  application-level integer minor units; timestamps use RFC 3339 UTC with fixed
  precision; binary uses base64url
  plus a type marker; UUIDs and enums use canonical lowercase/registered wire
  strings where their typed contract specifies that form; URLs use a typed URL
  profile and are not generically path-normalized.
- **Reasoning:** JCS solves deterministic JSON representation, not business
  equivalence. Typed and application normalization must be explicit and versioned.

## D6. Identity hash construction

- **Status:** frozen for `v1alpha1`.
- **Decision:**

  ```text
  canonical_bytes = UTF8(JCS(identity_preimage))
  hash_input = UTF8("mycelium.effect.v1\\n") || canonical_bytes
  digest = SHA-256(hash_input)
  effect_id = "mycelium:effect:v1:" || lowercase_hex(digest)
  ```

- **Limits:** Canonical preimage is limited to 64 KiB. Oversize input fails before
  claim with `IDENTITY_PREIMAGE_TOO_LARGE`.
- **Reasoning:** SHA-256 and lowercase hexadecimal are widely available and match
  the current runtime family. The identity is not a secret, so a keyed hash is not
  required. Hashes do not provide confidentiality.
- **Compatibility:** The current Python 64-character digest has no prefix and uses
  a different effective preimage/serializer. It must not be assumed equal.

## D7. Policy and contract versions

- **Status:** frozen for `v1alpha1`.
- **Decision:** `canonicalization_version`, `identity_version`, and
  `tool_contract_version` are identity members. `policy_version` is recorded and
  revalidated but excluded from identity. Protocol/envelope/runtime versions are
  transport or implementation metadata.
- **Consequences:** A policy change does not create a second effect. The engine
  evaluates policy for each new claim, but a terminal policy denial remains terminal
  for that effect ID. Reauthorization requires an explicit migration/operator action
  and an auditable link, normally with a new business request identity; it is never
  an automatic retry. A canonicalization, identity, or tool-contract change creates
  a new namespace and must never silently reinterpret an old record.

## D8. Provider keys and secrets

- **Status:** frozen for `v1alpha1`.
- **Decision:** Provider idempotency keys, references, and responses are not identity
  members. Provider keys are write-once evidence and are checked by the provider-key
  policy. Resolved credentials are prohibited from canonical input. Structured secret
  references are allowed only where the tool contract defines their stable identity.
- **Reasoning:** Provider keys describe a provider retry mechanism, not necessarily
  the logical operation. Secret references must not become a covert credential store.

## D9. Sidecar transport

- **Status:** approved prototype transport profile; not part of identity implementation.
- **Decision:** First experiment uses localhost HTTP with JSON and OpenAPI 3.1.
  Unix sockets are a hardening option; stdio and gRPC are deferred.
- **Reasoning:** HTTP and JSON are easiest to inspect and consume across languages.
  Localhost still requires authentication and tenant/application scoping.

## D10. Host provider execution

- **Status:** proposed, not part of identity implementation.
- **Decision:** The host performs the provider call initially and reports boundary,
  provider reference, completion, or ambiguity. The engine owns identity and state.
- **Consequence:** The provider-call crash windows remain. The sidecar cannot make
  an unrelated provider call atomic with its ledger or prevent direct provider
  bypass. Engine-owned provider workers are later scope.

## D11. Doctor and Verify

- **Status:** accepted boundary decision.
- **Decision:** Doctor and Verify remain evidence interfaces rather than ordinary
  identity operations. Future sidecar health/capability endpoints may expose
  equivalent diagnostics.
- **Consequence:** Passing them cannot prove all application calls use the engine.

## D12. Existing Python records and rollout

- **Status:** approved for the development-only prototype.
- **Decision:** Use a separate `legacy-python` namespace and a separate `identity-v1`
  namespace. Legacy records are never recomputed, overwritten, dual-written, or
  claimed through an identity-v1 ID. The prototype may inspect legacy records
  read-only, but it cannot execute or mutate them.
- **State treatment:** A legacy `COMMITTED` record may be returned as a legacy
  result by an explicitly legacy-scoped lookup. `ABORTED` remains non-executable.
  `UNKNOWN` is never treated as not-executed and cannot be aliased for execution.
  Active/in-flight records cannot be aliased. Operator-resolved records retain
  their original state and evidence and are read-only for the prototype.
- **Evidence:** Legacy request-to-effect mappings, provider references, stored
  results, argument fingerprints, and policy evidence are copied only as immutable
  source evidence. They are not substituted for the complete identity-v1 preimage.
  An automatic alias is rejected unless every identity-v1 member is reproduced
  with certainty and the source evidence is authoritative.
- **Audited alias:** A future migration authority may create one immutable,
  one-to-one alias containing `legacy_identity`, `identity_v1_identity`,
  `identity_v1_preimage_digest`, `authority`, `reason`, `timestamp`,
  `source_evidence`, `migration_version`, and `restrictions`. Source evidence is
  a redacted evidence reference or digest, never raw credentials or provider
  secrets. An alias never
  authorizes execution, never rewrites history, and cannot be removed or
  superseded. Conflicting aliases are rejected. Rollback disables the migration
  adapter and preserves the alias and both records.
- **Mixed versions:** Legacy workers remain on their namespace. During rollout,
  reads may be dual-read with explicit namespace labels, but writes are single-
  namespace and no automatic cross-namespace deduplication is attempted.

## D13. Typed decimal and URL profiles

- **Status:** approved for `jcs-1`.
- **Decision:** Generic exact decimals use `{"$type":"decimal","profile":"decimal-1","value":"..."}`
  with decimal-1 rules: ASCII digits, optional minus, no plus, exponent,
  whitespace, grouping, leading zeros, trailing fractional zeros, decimal point
  on integral values, or negative zero. Zero is `"0"`; `1.0` is rejected, not
  normalized. Maximum precision is 38 total digits and maximum scale is 18.
  Raw JSON numbers are not decimal quantities. Money normally uses integer minor
  units plus currency when the currency's minor unit is safe; provider-specific
  numeric formats remain tool-contract values.
- **URL decision:** URL-1 is a tagged value requiring an absolute UTF-8 `http` or
  `https` URL without credentials, fragment, controls, or non-ASCII DNS text.
  Scheme and DNS host are lowercased. Path case, query order and repetition,
  percent-encoding, explicit ports, and trailing slashes are preserved. No
  dot-segment, default-port, slash, query-sort, or provider normalization occurs.
  Unsupported schemes require a separately versioned profile.

## D14. Local authentication and principal binding

- **Status:** approved for the development-only prototype.
- **Decision:** Bind HTTP to loopback only and require a high-entropy bearer token
  of at least 256 bits delivered through an owner-readable (`0600`) file or
  inherited process channel. Send it only in the HTTP Authorization header. Never
  put it in argv, URLs, ordinary logs, or outcomes. Rotate it on restart,
  reject missing, invalid, or expired tokens, and use constant-time comparison.
  Unauthenticated localhost access is forbidden normatively. Unix-socket peer
  credentials are an optional hardening layer, not a substitute for the token in
  the HTTP profile. This authenticates a local client, not a human or production
  tenant.
- **Principal:** Each prototype instance has exactly one fixed tenant and one fixed
  application. The authenticated token binds both and permits an explicit tool /
  capability set. Client-supplied tenant/application fields are assertions only;
  missing or mismatched values are rejected with `TENANT_MISMATCH` or
  `APPLICATION_MISMATCH`. Storage and inspection are partitioned by that binding.
  Operator and migration actions require a distinct operator-authority token and
  are never accepted under an ordinary client token.
- **Errors:** `AUTHENTICATION_REQUIRED`, `AUTHENTICATION_INVALID`,
  `AUTHENTICATION_EXPIRED`, `TENANT_MISMATCH`, `APPLICATION_MISMATCH`, and
  `CAPABILITY_DENIED` are stable errors.

## D15. Policy reauthorization and migration

- **Status:** defined in v1; not required for the first claim-only prototype.
- **Decision:** Add `authorize_transition_reconsideration`. It records the original
  effect identity and policy version, new policy version, authenticated authorizer,
  reason, evidence reference, timestamp, destination/input digest, resulting
  business request identity, whether a new transition is created, the old/new
  linkage, and audit outcome.
- **Rules:** A committed or denied effect is never edited or reauthorized in place.
  An UNKNOWN effect requires reconciliation or explicit evidence and cannot be
  declared not-executed by this operation. Reauthorization creates a new linked
  transition, normally with a new business request ID, and evaluates current
  policy again. It does not imply execution permission. The original denial and
  all evidence remain visible. The operation requires distinct authority and is
  idempotent by its own migration request identity.
- **Prototype:** Claiming a reconsideration is rejected with
  `POLICY_REAUTHORIZATION_UNSUPPORTED` until an implementation explicitly enables
  the audited operation. No generic operator-resolution command may substitute for
  it.

## D16. Hostile clients and provider attestation

- **Status:** approved with an explicit deployment boundary.
- **Decision:** The development-only sidecar targets correct and fallible clients.
  Stable identity, sidecar duplicate suppression, drift detection, stale-fence
  rejection, conservative ambiguity, and fixed-tenant isolation are enforced under
  authenticated-client, durable-storage, and truthful-host assumptions. A hostile
  client can lie about provider completion or bypass the sidecar if it has provider
  credentials. Those guarantees are unsupported, and exactly-once execution is
  never claimed.
- **Attestation:** Provider attestation is not required for recording a provider
  reference or client-reported completion in the development profile, but neither
  is proof against a hostile client. Reconciliation requires a trusted read-only
  reconciler. Optional `attestation` evidence may include `type`, `issuer`,
  `subject_effect`, `provider_operation_ref`, `issued_at`, `payload_digest`,
  `verification_status`, `verifier`, and opaque provider evidence. Secrets and raw
  sensitive receipts are excluded from ordinary events. Hostile-client guarantees
  require provider evidence, sidecar-owned execution, or a trusted gateway and
  remain later deployment scope.

## D17. Final sidecar-readiness decision

- **Status:** approved for a development-only sidecar prototype.
- **Scope:** loopback HTTP with authenticated JSON, one fixed tenant and
  application per instance, engine-derived identity-v1 IDs, host-executed provider
  calls, optional read-only legacy inspection, and correct/fallible clients only.
- **Prohibited:** remote binding, unauthenticated localhost, multi-tenant use,
  automatic legacy migration, dual writes, provider credential exposure to an
  untrusted client, hostile-client guarantees, production reliability claims,
  and exactly-once claims. No implementation may guess at an unspecified profile.

## Stable errors

The identity layer uses these proposed machine-readable codes:

`INVALID_JSON`, `REQUEST_TOO_LARGE`, `NOT_FOUND`,
`UNSUPPORTED_CANONICALIZATION_VERSION`, `UNSUPPORTED_IDENTITY_VERSION`,
`UNSUPPORTED_VALUE_TYPE`, `DUPLICATE_OBJECT_KEY`, `INVALID_UNICODE`,
`NON_FINITE_NUMBER`, `NON_CANONICAL_NUMBER`, `INTEGER_OUT_OF_RANGE`,
`INVALID_TIMESTAMP`, `INVALID_URL`, `INVALID_BINARY`, `INVALID_DECIMAL`,
`NON_CANONICAL_DECIMAL`,
`SECRET_VALUE_PROHIBITED`, `IDENTITY_REQUIRED`, `IDENTITY_PREIMAGE_TOO_LARGE`,
`EFFECT_ID_MISMATCH`, `AUTHENTICATION_REQUIRED`, `AUTHENTICATION_INVALID`,
`AUTHENTICATION_EXPIRED`, `TENANT_MISMATCH`, `APPLICATION_MISMATCH`,
`CAPABILITY_DENIED`, and `POLICY_REAUTHORIZATION_UNSUPPORTED`.

All occur before claim where possible. A corrected request may be retried safely;
an effect ID mismatch must first be inspected because the client may be referring
to an existing operation.

## RFC disposition

The five former blockers are resolved by D12 through D17. The RFC is approved for
implementation of a development-only sidecar prototype within D17's restrictions.
Production deployment, remote multi-tenancy, automatic migration, hostile-client
protection, and provider-owned execution remain later scope rather than implicit
protocol guarantees.

## D18. Protocol v1alpha1 freeze

- **Status:** accepted and frozen.
- **Decision:** The first development-only wire revision is `v1alpha1`. The
  sidecar, OpenAPI document, TypeScript client, Go client, and protocol examples
  must advertise and require that exact value.
- **Independent versions:** The protocol revision does not rename or weaken
  `identity-v1` or `jcs-1`. A change to identity preimage or canonicalization
  semantics requires its own new namespace or profile as well as an appropriate
  protocol revision.
- **Compatibility:** The contents of `v1alpha1` are immutable. Breaking changes
  require a new revision such as `v1alpha2`; clients must fail closed when a
  server advertises an unsupported revision. Non-wire corrections may retain
  `v1alpha1`. Extensions remain subject to the existing critical and
  non-critical extension rules.
- **Release meaning:** Frozen means that implementations have a fixed target. It
  does not mean production-ready, stable `v1`, published, or supported for remote
  and multi-tenant deployment.
