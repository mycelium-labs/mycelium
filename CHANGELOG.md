# Changelog

## 1.2.0 (2026-06-30)

- `mycelium demo`: terminal demo of langgraph#7417 duplicate tool execution
- `mycelium init` defaults to LangGraph quickstart template; use `mycelium init --full` for all guards

## 1.1.1 (2026-06-30)

- PyPI description and README use plain language

## 1.1.0 (2026-06-30)

First public PyPI release as **`mycelium-runtime`** (`pip install mycelium-runtime`).

### Packaging
- PyPI distribution renamed from `mycelium-sdk` (name taken) to `mycelium-runtime`
- Python **3.10+** support (was 3.12-only in early releases)
- GitHub Actions publish workflow (tag `v*` → PyPI)

### Ledger storage backends
- **File**: `fcntl` locking for multi-process safety on a single host
- **Redis**: atomic `SET NX` claim + in-flight TTL (multi-worker)
- **Postgres**: `INSERT ... ON CONFLICT` claim (audit/compliance)
- Optional extras: `mycelium-runtime[redis]`, `mycelium-runtime[postgres]`

## 1.0.0 (2026-06-29)

First production release. Context guards, tool boundaries, and action idempotency with YAML-first integration.

### Requirements
- Python **3.10+** (tested on 3.10, 3.11, 3.12, 3.13)

### Context
- `@protect` / `protect_sync`: TTL cache with per-entity keys
- `Session`: per-run cache isolation
- `MessageValidator`: broken transcript detection and repair
- `HistoryGuard`: token limits and silent drop detection

### Tool boundaries
- `@bounded` / `bounded_sync`: input/output validation and scope gates
- `ToolRegistry`: allowlist enforcement
- `ToolRunner`: structured LLM retry on boundary failures

### Action idempotency
- `ActionLedger` / `@ledger`: tool-level idempotency
- `TaskLedger` / `@task_ledger`: task-level idempotency
- `StateFlush`: partial state persistence on cancel/disconnect/error
- `AuditReceipt`: HMAC-signed tamper-evident action receipts

### Developer experience
- YAML config with global sections: `action_ledger`, `task_ledger`, `state_flush`, `audit_receipt`
- `mycelium init`: scaffold `mycelium.yaml` from bundled templates (PyPI users)
- `config.instrument(module)`: wrap tools and tasks in one call
- `config.prepare_messages()`: message validation + history guard + auto state recording
- `config.run(run_id)`: Session + StateFlush combined
- `registry.auto: true`: allowlist from configured tools
- `ledger: true` inherits global storage settings
