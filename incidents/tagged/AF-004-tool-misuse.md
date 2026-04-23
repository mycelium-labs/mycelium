# AF-004 — Tool Misuse
**One-line:** The agent calls a tool with invalid inputs or outside its intended scope, causing silent failures or side effects.
**Detection signal:** Tool called with arguments that violate its declared schema or preconditions.
**Runtime fix:** Mycelium enforces precondition and postcondition checks on every tool call before execution.

## Incidents
<!-- one per incident, dated, linked, tagged -->
