# AF-007 — Premature Termination
**One-line:** The agent stops before the task is complete and presents the partial state as final, causing the user or downstream system to act on an incomplete result.
**Detection signal:** Agent emits a completion / "done" / final-output signal while declared subtasks, planned steps, or required fields remain unresolved or unverified.
**Runtime fix:** Mycelium enforces a completion contract — every declared subtask in the agent's plan must produce an explicit success or failure event before the terminal output is allowed to propagate.

**In scope (tag this AF-007):**
- Agent reports task complete but only some subtasks ran
- Agent returns a polished final answer that omits required pieces of work
- Multi-step plan exits early without acknowledging the unfinished steps
- Agent fabricates a "done" status to escape a hard problem

**Out of scope (do NOT tag AF-007):**
- Loud crashes, raised exceptions, process exits — covered by standard runtime monitoring (Sentry-class tools)
- Missing retry / HTTP recovery logic in the model client — engineering plumbing concern, not a behavioral failure
- User explicitly cancels mid-task

The discriminator is **silence vs noise**: if the user/system can clearly see the agent failed, it's not AF-007. If the user/system *thinks the work was done* when it wasn't, it is.

## Incidents
<!-- one per incident, dated, linked, tagged -->
