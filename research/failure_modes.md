# Agent Failure Modes - Index

Canonical specs live in `incidents/tagged/AF-*.md`.

**HF frequency** = occurrences of each AF-* in `predictions/*.jsonl` on `ndileep/mycelium-agent-failures` (compound labels count each mode once). Snapshot: **2026-05-03** - regenerate via `python scripts/analyze_af_frequency.py`.

**v1 SDK** - see `research/v1-scope.md`: focus **AF-006**, **AF-004**, **AF-002** first.

**AF-006 sub-types:** expanded checklist and per-class SDK coverage (implemented / partial / none) in `research/context-corruption-taxonomy.md`.

| ID | Name | One-line | HF freq | v1 |
|----|------|----------|--------:|:--:|
| AF-001 | Hallucination cascade | The agent confidently acts on fabricated facts, compounding errors across tool calls. | 36 | |
| AF-002 | Observability black hole | Consequential actions leave no trace - auditing/debugging impossible. | 304 | ✓ |
| AF-003 | Infinite reasoning loops | Same reasoning cycle repeats; no progress, token burn. | 218 | |
| AF-004 | Tool misuse | Tool calls with invalid inputs or outside intended scope; silent failure or wrong side effects. | 575 | ✓ |
| AF-005 | Goal misalignment | Optimizes for a proxy objective, not user intent. | 177 | |
| AF-006 | Context corruption | Stale, truncated, or poisoned context → false picture of the world. | 501 | ✓ |
| AF-007 | Premature termination | Stops before done; presents partial state as final. | 415 | |
| AF-008 | Cascading permission | Narrow permissions escalate transitively beyond intent. | 9 | |
| AF-009 | Instruction injection | Untrusted content hijacks instructions. | 22 | |
