# AF-006 - Context Corruption
**One-line:** Stale, truncated, or poisoned context causes the agent to act on a false picture of the world.
**Detection signal:** Agent references entities or states not present in the current context window.
**Runtime fix:** Mycelium validates context integrity at each reasoning step and flags stale references.

## Incidents
<!-- one per incident, dated, linked, tagged -->
