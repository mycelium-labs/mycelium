# AF-001 — Hallucination Cascade
**One-line:** The agent confidently acts on fabricated facts, compounding errors across tool calls until the task is unsalvageable.
**Detection signal:** Tool outputs contradict prior tool outputs; agent cites data never returned in context.
**Runtime fix:** Mycelium cross-validates tool return values against context before the next action is permitted.

## Incidents
<!-- one per incident, dated, linked, tagged -->
