# Mycelium

[![PyPI version](https://img.shields.io/pypi/v/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)
[![Python](https://img.shields.io/pypi/pyversions/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)
[![Downloads](https://static.pepy.tech/badge/mycelium-runtime)](https://pepy.tech/project/mycelium-runtime)

**The reliability layer for AI agents.**

Your agent decides what to do. Mycelium makes tool actions reliable across
their full lifecycle:

- **Before execution:** validate inputs, scope, destinations, secrets,
  authorization, and current facts.
- **During the run:** control retries, concurrency, crashes, loops, budgets,
  context, and completion.
- **After an attempt:** establish what happened, return stored outcomes,
  reconcile uncertainty, and record evidence.

Wrong answers are recoverable. Wrong actions are expensive. Mycelium sits
between your agent and its tools to block invalid actions, control execution,
and preserve trustworthy outcomes.

It is not a tracer or dashboard. It controls the action path itself.

The engine is written in Python, but the doorway into it is language-neutral.
Python applications use the runtime directly. TypeScript, Go, and any runtime
that can send HTTP/JSON can use the same engine through a small local sidecar.

The Python API follows semantic versioning. The `v1alpha1` sidecar protocol is
for development and is not yet a stable production contract.

Early design-partner use: **live outbound-email lane** (Week 1: 25 ledgered sends, 0 duplicates). This is evidence for one execution-control lane, not the limit of the product. Not a public logo; the interactive sandbox is separate.

## What it protects

| Risk | What Mycelium does |
|------|--------------------|
| Invalid or unsafe tool calls | Validates arguments, paths, destinations, and tool allowlists before execution |
| Expired or missing authority | Re-checks scope, destructive grants, current facts, and time-bound permissions |
| Duplicate or uncertain effects | Coordinates retries and concurrent workers, returns stored outcomes, and reconciles ambiguous attempts |
| Runaway agents | Stops repeated action loops and enforces time, step, token, and cost budgets |
| Stale context | Validates message, history, and state before the next action |
| False completion | Refuses “done” while required work remains open |
| Missing evidence | Records durable outcomes and optional signed receipts |

## Who it's for

Developers running **agents with side-effect tools** on **LangGraph, CrewAI,
plain Python, TypeScript, Go, or another runtime that can speak HTTP/JSON**.

The authoritative engine requires Python 3.10+. Python applications can use
YAML, `mycelium run`, or decorators. Other languages connect through the
development sidecar protocol.

## Works with your stack

Python applications can use YAML, decorators, or a manual API. For other
languages, the sidecar runs Mycelium as a small local server beside your
application:

```text
TypeScript · Go · Java · Rust · any HTTP client
                         ↓ HTTP/JSON
                local Mycelium sidecar
                         ↓
             authoritative Python engine
```

The application asks the sidecar whether an action may run, reports when the
provider call may have started, and records its result. The sidecar owns action
identity, claims, leases, fencing, state transitions, and recovery decisions.
Clients do not reimplement those safety rules.

Published experimental clients:

```bash
npm install @mycelium-labs/sidecar-client@experimental
go get github.com/mycelium-labs/mycelium/clients/go@v0.1.0
```

Every other language can use the same authenticated OpenAPI contract directly.
The sidecar protocol is currently for trusted local development. See the
[protocol overview](sdk/docs/spec/README.md), [TypeScript
client](clients/typescript/README.md), and [Go client](clients/go/README.md).

## How it works

1. The model proposes a tool call.
2. Mycelium applies the checks configured for that tool and run.
3. It either runs the tool, returns a stored result, waits, reconciles an
   uncertain outcome, or stops safely.
4. It records the decision and outcome for recovery and audit.

Mycelium complements tracers and approval systems; it does not replace them.
See the [SDK reference](sdk/README.md) for configuration details and the
[failure and threat model](sdk/docs/FAILURE_AND_THREAT_MODEL.md) for exact
guarantees and limits.

## Quickstart

```bash
pip install mycelium-runtime
mycelium demo
mycelium init
mycelium run --config mycelium.yaml -- python -m my_app
```

`mycelium init` creates a starter configuration for one tool. Point it at your
callable, describe whether the tool reads or changes external state, and enable
the controls your workflow needs. Use SQLite for a durable single-process setup,
or Redis/Postgres for multiple workers.

Prefer agent-assisted setup:

```bash
mycelium skills install
```

Then ask your coding agent: **“Set up Mycelium in this project.”** The bundled
[`mycelium-setup`](.agents/skills/mycelium-setup/SKILL.md) skill inventories
tools, updates the configuration, wires the runtime boundary, and runs Doctor
and Verify.

For non-Python applications, run the local sidecar and use the TypeScript, Go,
or OpenAPI client:

```bash
mycelium sidecar serve --config /absolute/path/sidecar.yaml
```

See the [non-Python setup](sdk/README.md#typescript-go-and-other-languages) for
the token and minimal sidecar configuration. Only calls routed through
Mycelium are protected. See the
[full SDK reference](sdk/README.md) for framework integrations, storage,
configuration, and manual APIs.

## Docs

- **Handbook:** https://mycelium-labs.github.io/ ([website repo](https://github.com/mycelium-labs/mycelium-labs.github.io))
- **Try in 5 minutes:** https://mycelium-labs.github.io/try.html
- **Sandbox demo:** [mycelium-labs/mycelium-labs.github.io/sandbox](https://github.com/mycelium-labs/mycelium-labs.github.io/tree/main/sandbox)
- **Full API reference:** [sdk/README.md](sdk/README.md)
- **Language-neutral protocol:** [sdk/docs/spec/README.md](sdk/docs/spec/README.md)
- **Experimental clients:** [TypeScript](clients/typescript/README.md) · [Go](clients/go/README.md)
- **Doctor vs Verify:** `mycelium doctor` inspects configuration; `mycelium verify` runs synthetic failure scenarios. Neither proves a real provider is correct.
- **Release policy & checklist:** [sdk/docs/RELEASE.md](sdk/docs/RELEASE.md) (batch; calm over velocity)
- **Security policy & private reporting:** [SECURITY.md](SECURITY.md)
- **PyPI:** https://pypi.org/project/mycelium-runtime/

## License

MIT. See [LICENSE](LICENSE).
