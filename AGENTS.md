# Agent conventions for the Mycelium repo

Rules any AI coding agent (Cursor, Claude Code, etc.) must follow when working
in this repository. Short, mandatory, no exceptions.

## Auto-update `LOG.md` at every session end

Nandana is the sole operator. She does not want to manually ask for log
entries. Every agent working in this repo is responsible for keeping
`LOG.md` up to date.

### When to append a new entry

Append a new entry to `LOG.md` when any of the following is true and a new
entry has not yet been added this session:

- The user's reply is purely an acknowledgment (`ok`, `cool`, `nice`,
  `got it`, `thanks`, `hmm`, `lol`, `later`, `bye`, a single emoji, etc.).
- A concrete task the user asked for has been completed and the user has
  moved to a new unrelated topic.
- The user says "I'm done", "see you later", "that's it", or similar.
- It has been more than ~8 substantive turns since the last entry.
- The user explicitly asks for a log entry.

Do NOT wait for the user to ask. The rule exists *because* she does not
want to ask.

### What an entry looks like

Reverse-chronological (newest on top). Each entry is **4 bullets, no more**:

```
## YYYY-MM-DD - short title (<= 8 words)
- did: <what happened this session>
- found: <what we learned / decided / discovered> (optional)
- now: <current state of the repo / project>
- next: <the most valuable next step>
```

Rules for writing entries:

- Max 6 lines total per entry. If you need more, you're over-logging - put
  the detail in `research/`, `incidents/`, or `docs/` and link from the log.
- No emojis. No marketing language. Plain status.
- Write in past tense for `did` / `found`, present tense for `now`,
  imperative for `next`.
- If multiple sessions happen on the same day, append `- AM` / `- evening`
  / `- session 2` to the title rather than merging entries.
- Dates are the real calendar date. If you don't know it, ask or check
  `date` / the environment before writing the entry.

### Where to write

The entry goes at the top of `LOG.md`, directly below the `---` separator.
Do not modify previous entries. If a previous entry was wrong, add a new
entry noting the correction.

### When NOT to append

- The session was purely conversational/strategic and no decisions were
  made or code changed. In that case, skip the entry.
- The user is mid-task and has not returned control (you are inside a
  multi-step execution).

### Output contract

When you append a log entry at session end, briefly mention it in your
reply so the user sees it happened, e.g.:

> Logged today's session in `LOG.md`.

One line, nothing more. Do not quote the entry back to her.

---

Other conventions live under each folder's own README (`sdk/`, `research/`,
`incidents/`, etc.) as they are written.
