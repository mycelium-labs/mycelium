# Mycelium SDK — Documentation Index

All documentation for the Mycelium SDK, organized by audience.

---

## By audience

**Engineers integrating the SDK**
1. [README.md](README.md) — Quick start, installation, usage examples
2. [INTEGRATION_CHECKLIST.md](INTEGRATION_CHECKLIST.md) — Step-by-step integration guide
3. [CHANGELOG.md](CHANGELOG.md) — What's in each release, API reference

**Security reviewers**
1. [PROOF_SUMMARY.md](PROOF_SUMMARY.md) — End-to-end proof, one section per failure mode
2. [AF006_PROOF.md](AF006_PROOF.md) — Test inventory (47 named tests, coverage matrix)
3. [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006) — Real-world validation (507 real failures, reproducers)

**Architects / researchers**
1. [PROOF_SUMMARY.md](PROOF_SUMMARY.md) — Formal invariants per failure mode
2. `mycelium/protect.py` — Complete public API implementation (~260 lines)
3. [AF006_PROOF.md](AF006_PROOF.md) — Coverage matrix, migration history

---

## All files

### SDK documentation

| File | Purpose |
|------|---------|
| [README.md](README.md) | Quick start, `@protect` / `Session` examples |
| [CHANGELOG.md](CHANGELOG.md) | Release notes — what shipped, what's not in scope |
| [AF006_PROOF.md](AF006_PROOF.md) | Coverage matrix, 47-test inventory, failure mode table |
| [PROOF_SUMMARY.md](PROOF_SUMMARY.md) | End-to-end proof — mechanism + invariant + test evidence per FM |
| [INTEGRATION_CHECKLIST.md](INTEGRATION_CHECKLIST.md) | Integration guide with framework examples |

### External documentation

| Resource | Purpose |
|----------|---------|
| [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006) | Reference test repo — 679 tests across all layers |
| [agent-test-AF006/TESTING.md](https://github.com/mycelium-labs/agent-test-AF006/blob/main/TESTING.md) | How to run each test layer |
| [agent-test-AF006/AF006_PROOF.md](https://github.com/mycelium-labs/agent-test-AF006/blob/main/AF006_PROOF.md) | Implementation history, design decisions, test inventory |
| [agent-test-AF006/RELEASES.md](https://github.com/mycelium-labs/agent-test-AF006/blob/main/RELEASES.md) | Test suite release history |
| [agent-test-AF006/AF006_FAILURE_CATALOG.md](https://github.com/mycelium-labs/agent-test-AF006/blob/main/AF006_FAILURE_CATALOG.md) | 507 real failures with GitHub issue links |

### Source

| File | Purpose |
|------|---------|
| `mycelium/protect.py` | Public API: `@protect`, `protect_sync`, `Session` |
| `mycelium/protections/context_corruption.py` | Internal step-based cache (not public API) |
| `mycelium/core/runtime_context_corruption.py` | Internal runtime (not public API, will be removed) |

### Tests (SDK)

| File | What it covers |
|------|---------------|
| `tests/test_protect_failure_modes.py` | `@protect` + `Session` unit tests |
| `tests/test_context_corruption.py` | Internal `ContextCache` mechanics |
| `tests/test_runtime_context_corruption.py` | Internal runtime integration |
| `tests/test_stress_context_corruption.py` | Stress / scalability |

---

## Common questions

**"How do I use the SDK?"** → [README.md](README.md)

**"What does each release contain?"** → [CHANGELOG.md](CHANGELOG.md)

**"Is AF-006 really prevented?"** → [PROOF_SUMMARY.md](PROOF_SUMMARY.md)

**"What are all the tests?"** → [AF006_PROOF.md](AF006_PROOF.md) (SDK) and [agent-test-AF006 AF006_PROOF.md](https://github.com/mycelium-labs/agent-test-AF006/blob/main/AF006_PROOF.md) (test repo)

**"How do I run the tests?"** → [agent-test-AF006/TESTING.md](https://github.com/mycelium-labs/agent-test-AF006/blob/main/TESTING.md)

**"What real failures does this address?"** → [AF006_FAILURE_CATALOG.md](https://github.com/mycelium-labs/agent-test-AF006/blob/main/AF006_FAILURE_CATALOG.md)

---

## Test counts (current)

```
SDK unit tests                  ~60   sdk/tests/
Direct integration (FM1–FM7)     47   agent-test-AF006/tests/test_af006_coverage.py
Property-style parametrized      22   agent-test-AF006/tests/test_af006_properties.py
Adversarial                      12   agent-test-AF006/tests/test_af006_adversarial.py
Scenario reproducers             30   agent-test-AF006/tests/test_af006_scenario_reproduction.py
Framework integration            70   agent-test-AF006/tests/test_af006_framework_integration.py
Framework e2e (per-framework)   ~80   agent-test-AF006/tests/framework_e2e/
Real agent scenarios            ~20   agent-test-AF006/tests/test_af006_real_scenarios.py
LiveKit #5408                    10   agent-test-AF006/tests/real_issues/
Real failure metadata           507   agent-test-AF006/tests/test_af006_real_failures.py
                                ────
Total                          ~858
```
