# AF-009 — Instruction Injection
**One-line:** Malicious content in the environment hijacks the agent's instructions, redirecting it to unintended actions.
**Detection signal:** Agent behavior diverges from original task after processing external content (web, files, tool output).
**Runtime fix:** Mycelium sanitizes and isolates external content from the instruction context before reasoning.

## Incidents
<!-- one per incident, dated, linked, tagged -->
