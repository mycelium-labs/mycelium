# AF-007 — Premature Termination
**One-line:** The agent stops before the task is complete, returning a partial result as if it were final.
**Detection signal:** Agent emits a completion signal while required subtasks remain incomplete or unverified.
**Runtime fix:** Mycelium checks all declared subtasks are resolved before allowing a completion signal to propagate.

## Incidents
<!-- one per incident, dated, linked, tagged -->
