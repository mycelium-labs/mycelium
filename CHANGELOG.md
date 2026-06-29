# Changelog

## 1.0.0 — 2026-06-29

First production release. Ships three failure modes with YAML-first integration.

### AF-006 — Context corruption
- `@protect` / `protect_sync` — TTL cache with per-entity keys
- `Session` — per-run cache isolation
- `MessageValidator` — broken transcript detection and repair
- `HistoryGuard` — token limits and silent drop detection

### AF-004 — Tool boundary enforcement
- `@bounded` / `bounded_sync` — input/output validation and scope gates
- `ToolRegistry` — allowlist enforcement
- `ToolRunner` — structured LLM retry on boundary failures

### AF-002 — Observability black hole
- `ActionLedger` / `@ledger` — tool-level idempotency
- `TaskLedger` / `@task_ledger` — task-level idempotency
- `StateFlush` — partial state persistence on cancel/disconnect/error
- `AuditReceipt` — HMAC-signed tamper-evident action receipts

### Developer experience
- YAML config with global sections: `action_ledger`, `task_ledger`, `state_flush`, `audit_receipt`
- `config.instrument(module)` — wrap tools and tasks in one call
- `config.prepare_messages()` — AF-006 guards + auto state recording
- `config.run(run_id)` — Session + StateFlush combined
- `registry.auto: true` — allowlist from configured tools
- `ledger: true` inherits global storage settings

### Proof
- Issue-linked fixtures for AF-006, AF-004, and AF-002
- `proof/run_demo.py` human-readable demo
