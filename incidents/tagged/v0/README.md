# v0 — Hand-tagged failure-mode corpus

This is the **proprietary catalog**. Tagged by hand against `AF-001 … AF-009`.
Every entry here is a deliberate human judgment about how an agent failed —
this is what we'll learn from when designing runtime protections.

This data lives in git, **not** Hugging Face. Raw issues are public; our
tags are not.

## Files

- `queue.jsonl` — deterministic random sample (5 issues × 10 repos = 50). Built once, never edited.
- `tagged.jsonl` — your tags, appended one per call. Each line is one decision.

## Schema (`tagged.jsonl`)

```json
{
  "id": "langchain-ai/langchain#12345",
  "repo": "langchain-ai/langchain",
  "number": 12345,
  "url": "https://github.com/...",
  "title": "...",
  "tagged_at": "2026-04-25T18:00:00+00:00",
  "tagged_by": "ndileep",
  "status": "tagged | skip | not-a-failure",
  "labels": ["AF-003", "AF-004"],
  "confidence": "high | medium | low | null",
  "evidence": "1-line quote or signal that justifies the tag",
  "notes": "free-text"
}
```

## How to tag

```bash
# one-time, builds queue.jsonl from the HF corpus
python scripts/tag_next.py build-queue

# tag the next un-tagged issue (run this 50 times, or whenever you have 5 min)
python scripts/tag_next.py

# see progress + label distribution
python scripts/tag_next.py status
```

## The taxonomy (read this before tagging)

Each AF-* spec lives next door at `incidents/tagged/AF-00N-*.md`. Read all
nine **once** before your first session. They're 4 lines each.

The script will show you the one-liner for each AF every time, but the spec
files have the canonical `Detection signal` and `Runtime fix` you'll need
when in doubt.

## Decision rules — when in doubt

**Multi-tag is fine.** If an issue is "agent looped because tool returned bad
data" → tag both `AF-003` (loop) and `AF-004` (tool misuse). The whole point
is to find the *combinations* that recur.

**Skip vs not-a-failure.** Skip = "I can't tell from this issue alone."
Not-a-failure = "this is a feature request / install bug / docs typo, not an
agent reliability issue." Both are valuable signal. We expect 30-50% of
random GitHub issues to be `not-a-failure` — that itself tells us how noisy
public data is.

**Evidence is required for tagged entries.** Force yourself to quote *one
line* from the issue that supports the tag. If you can't find one, you're
guessing — skip instead.

**Confidence.** `high` = the issue body explicitly describes the failure.
`medium` = strong inference from symptoms. `low` = pattern-matching, would
want a second opinion.

## What "done" looks like

50 tagged entries → run `tag_next.py status` → look at the label histogram.
The top 3 most-common AF-* are the v1 protection scope (issue #4 in the M1
milestone).
