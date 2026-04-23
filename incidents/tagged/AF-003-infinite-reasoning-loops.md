# AF-003 — Infinite Reasoning Loops
**One-line:** The agent cycles through the same reasoning steps indefinitely, burning tokens and time without progress.
**Detection signal:** Action hashes repeat within a sliding window; step count exceeds threshold with no new state.
**Runtime fix:** Mycelium maintains an action-hash ring buffer and halts execution on detected repetition.

## Incidents
<!-- one per incident, dated, linked, tagged -->
