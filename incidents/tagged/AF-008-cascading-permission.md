# AF-008 — Cascading Permission
**One-line:** An agent granted narrow permissions escalates them transitively, acquiring capabilities far beyond original intent.
**Detection signal:** Tool calls requesting permissions not present in the original grant; scope creep across steps.
**Runtime fix:** Mycelium enforces permission boundaries at each step; no transitive escalation without explicit re-grant.

## Incidents
<!-- one per incident, dated, linked, tagged -->
