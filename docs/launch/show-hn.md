# Show HN — draft

Copy/paste and edit before posting. Post Tuesday–Thursday morning US time if you can.

---

## Title (pick one)

- **Show HN: Mycelium – runtime guards that stop agents from double-executing tools on retry**
- **Show HN: We scraped 500+ agent failure GitHub issues and shipped prevention guards**
- **Show HN: Idempotency keys for AI agent tool calls (Python)**

---

## Body

I kept seeing the same agent bugs in GitHub issues — not model quality problems, but infrastructure:

1. **Tool runs twice on retry** — LangGraph Cloud redispatches a long tool call while the first is still running ([langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417)). Duplicate cost and duplicate side effects.

2. **Broken tool-call transcripts** — orphan tool results, duplicate IDs ([langgraph#7117](https://github.com/langchain-ai/langgraph/issues/7117)). The model acts on garbage context.

3. **Stale cached tool data** — agent confidently uses outdated fetch results.

Observability tools (Langfuse, etc.) show you what happened *after*. I wanted guards that prevent these *during* execution — no extra LLM calls, just deterministic checks.

**Mycelium** is a Python library for that:

```bash
pip install mycelium-runtime   # Python 3.10+
mycelium init
```

- **Idempotency ledgers** — same `tool_call_id` / `request_id` won't execute the side effect twice (file, Redis, or Postgres storage)
- **Tool boundaries** — validate inputs/outputs and allowlists before tools run
- **Context guards** — TTL cache, message repair, history limits

Framework-agnostic — raw message lists and plain Python functions. Works alongside LangGraph, CrewAI, or a hand-rolled tool loop.

We built a taxonomy from 500+ tagged GitHub issues across LangChain/CrewAI/AutoGen and linked proof tests to specific issues (e.g. our test reproduces the langgraph#7417 duplicate-execution pattern).

**Try it:**

```python
from mycelium import ledger_sync

@ledger_sync()
def send_payment(amount: float, recipient: str, tool_call_id: str | None = None):
    return charge(amount, recipient)

# Second call with same tool_call_id returns without re-charging
```

PyPI: https://pypi.org/project/mycelium-runtime/  
LangGraph guide: `docs/integrations/langgraph.md` in the repo

Early release — feedback welcome. What failure modes are you hitting in production that we should prioritize?

---

## First comment (post immediately after submission)

Happy to answer questions. Quick clarifications:

- **Not a tracing product** — use Langfuse/Helicone for dashboards; Mycelium is runtime prevention
- **Requires Python 3.10+** (macOS system Python 3.9 won't see the package on pip)
- **Redis backend** for multi-worker: `pip install 'mycelium-runtime[redis]'`

---

## X / LinkedIn thread (shorter)

**Tweet 1:**  
Agents fail in boring, expensive ways: tool runs twice on retry, stale cache, broken tool-call JSON. Observability shows you after. We shipped runtime guards before the LLM runs.

**Tweet 2:**  
`pip install mycelium-runtime && mycelium init`

Idempotency keyed on tool_call_id. Reproduces patterns from real issues like langgraph#7417.

**Tweet 3:**  
Not Langfuse. Not a framework fork. Plain Python + YAML. Early — would love feedback from anyone running agents in prod.

---

## Checklist before posting

- [ ] Push PyPI 1.1.1 (plain-language README, no internal taxonomy codes)
- [ ] PyPI page looks right: https://pypi.org/project/mycelium-runtime/
- [ ] GitHub repo public (or at least docs + integrations visible)
- [ ] Test install on fresh Python 3.12 venv
- [ ] Block 2 hours to reply to comments
