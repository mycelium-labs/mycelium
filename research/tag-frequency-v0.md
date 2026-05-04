# v0 hand-tag - label frequency (50-issue sample)

Source: `incidents/tagged/v0/queue.jsonl` (deterministic sample, 5 × 10 repos).
Human-reviewed tags in `incidents/tagged/v0/tagged.jsonl`. Detailed rationale lives in
`incidents/tagged/v0/proposed.md`.

## Counts (AF labels on real behavioral failures in this slice)

| AF-ID | Theme | Count (approx.) | Notes |
|-------|--------|-------------------|--------|
| AF-006 | Context / memory corruption | 5 | Dominant signal in v0 |
| AF-002 | Observability black hole | 2 | Mechanism + incident shapes |
| AF-009 | Instruction injection | 1 | Often overlaps AF-006 (e.g. crewAI #5057) |
| AF-001, 003, 004, 005, 007, 008 | - | **0** in this random draw | Not “never happens”; small-N noise |

**Headline:** In this 50-issue slice, actionable agent-level failures cluster strongly on **AF-006** and **AF-002**. Use issue **#4** to translate frequency into v1 product scope.

## Phenomena that did not map cleanly to AF-001…AF-009

These appeared often as **negatives** (`not-a-failure`) or borderline - useful for taxonomy hygiene, not AF IDs:

1. **Vendor / integration pitches** - roadmap spam, not agent failure mechanics.
2. **Pure library / API bugs** - incorrect types, install issues, “except: pass” plumbing - important but **not** “agent chose wrong behavior” under our AF semantics (often tagged `n`).
3. **Hypothetical RFCs vs reproducible incidents** - same *shape* as AF-006/AF-002 in prose but no incident; tagged low confidence or `n` per reviewer judgment.

See borderline notes in `incidents/tagged/v0/proposed.md` § Borderline calls.
