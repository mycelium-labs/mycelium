# AF-005 — Goal Misalignment
**One-line:** The agent optimizes for a proxy objective that diverges from the user's actual intent.
**Detection signal:** Task completion criteria satisfied in logs but user intent demonstrably unmet.
**Runtime fix:** Mycelium compares final state against declared success conditions before marking a task complete.

## Incidents
<!-- one per incident, dated, linked, tagged -->
