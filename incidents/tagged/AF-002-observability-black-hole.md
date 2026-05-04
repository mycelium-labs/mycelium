# AF-002 - Observability Black Hole
**One-line:** The agent takes consequential actions with no trace, making post-hoc debugging or auditing impossible.
**Detection signal:** No structured logs emitted during a multi-step task; tool calls appear in output but not in trace.
**Runtime fix:** Mycelium enforces mandatory trace emission on every tool invocation; untraced calls are blocked.

## Incidents
<!-- one per incident, dated, linked, tagged -->
