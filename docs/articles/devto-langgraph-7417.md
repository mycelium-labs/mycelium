---
title: Duplicate tool execution in LangGraph (#7417) and a Python fix
published: false
tags: python, ai, langgraph, agents, opensource
series:
---

Your agent didn't hallucinate. **The tool ran twice.**

On LangGraph Cloud, a long-running tool call can be **redispatched while the original is still running**. Both complete. You pay twice. Side effects run twice — payments, emails, API writes, subagent runs.

This is [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417). It's not a model quality bug. It's an infrastructure gap: **no durable record that the tool call is already in flight.**

Here's the failure class, why checkpointing alone doesn't save you, and a five-line Python fix.

---

## The bug in plain English

1. Your graph calls `subagent_task` with a 5-minute job.
2. After ~3 minutes, LangGraph Cloud's runtime decides the worker looks stale.
3. It **redispatches the same node** from checkpoint.
4. The first invocation is still running.
5. Both finish successfully. **Duplicate work. Duplicate cost. Duplicate side effects.**

The LLM didn't choose to call the tool twice. The **runtime** did.

> Pattern from the issue: *"A long-running tool call is redispatched because the runtime has no durable record that the original is still in flight."*

Same class of failure shows up in [crewAI#5802](https://github.com/crewAIInc/crewAI/issues/5802) (task retry re-executes side-effect tools). The fix is the same shape everywhere: **claim before execute, in durable storage.**

---

## Why checkpointing isn't enough

LangGraph checkpointing saves **graph state**. It does not automatically make **tool side effects** idempotent.

When a node replays or redispatches:

- In-memory flags are gone (new worker, new process).
- Graph state may say "tool pending" but your payment API doesn't know that.
- Retrying the node runs your Python function again unless **you** guard it.

Contributors on #7417 converged on the same pattern:

1. Derive a stable **`request_id`** from the tool call (LangGraph's `tool_call_id` works).
2. **Atomically claim** it in durable storage *before* the side effect runs.
3. On redispatch, return the cached or in-flight result — **don't execute again**.

That claim has to happen **outside** the framework, in storage that survives worker restarts: file, Redis, or Postgres.

---

## Reproduce it locally (no LangGraph required)

We ship a terminal demo that reproduces the failure class from a real issue fixture:

```bash
pip install mycelium-runtime
mycelium demo
```

You'll see:

```
[1/2] Baseline: unguarded redispatch (failure class)
  [EXECUTING] subagent_task({'task': 'analyze_market', ...})
  [EXECUTING] subagent_task({'task': 'analyze_market', ...})
Executions: 2
PASS: duplicate side effect reproduced (this is the bug)

[2/2] Guarded: transition envelope (v1.3)
  [EXECUTING] subagent_task({'task': 'analyze_market', ...})
Executions: 1
side_effect_class: subagent
PASS: redispatch resolved existing transition, side effect ran once
```

Same assertions as our [proof test on GitHub](https://github.com/mycelium-labs/mycelium).

---

## The fix: init + apply

[Mycelium](https://github.com/mycelium-labs/mycelium) is an open-source runtime guard library. Framework-agnostic — plain Python, no LangGraph import required.

```bash
mycelium init
```

```python
from mycelium import load_config

config = load_config("mycelium.yaml")

@config.apply
def subagent_task(task: str, duration_seconds: int = 0) -> dict:
    return run_slow_subagent(task)

# LangGraph passes tool_call_id — pass it through on each invocation
result = subagent_task(
    task="analyze_market",
    duration_seconds=300,
    tool_call_id=call["id"],  # same id → resolve existing transition
)
```

What the transition envelope does:

1. Classify the tool (`side_effect_class: subagent`) and hash a durable transition key.
2. Before your function runs, claim that key in a ledger.
3. If the same transition is already **in-flight** or **completed**, resolve the existing outcome instead of re-executing.
4. Your side effect runs **once**.

---

## What `mycelium init` scaffolds

```yaml
transition:
  agent_id: my-agent
  policy_version: "2026.07.1"

action_ledger:
  storage: file
  path: ./mycelium-ledger.json
  tools:
    - subagent_task

tools:
  subagent_task:
    side_effect_class: subagent
```

---

## Multi-worker / LangGraph Cloud: use Redis

File storage only works on a single machine. Cloud deploys need a **shared ledger**:

```bash
pip install 'mycelium-runtime[redis]'
export MYCELIUM_REDIS_URL=redis://localhost:6379/0
```

```yaml
action_ledger:
  storage: redis
  url_env: MYCELIUM_REDIS_URL
  prefix: mycelium:action:
  in_flight_ttl: 3600
  tools:
    - subagent_task
```

All workers see the same in-flight claim.

---

## What this is not

| | Langfuse / Helicone | Mycelium |
|--|--|--|
| **When** | After the run | Before / during side effects |
| **What** | Traces, dashboards | Idempotency keys, ledgers |
| **Goal** | See what happened | Prevent duplicate execution |

Use both if you want traces **and** guards. Mycelium is **runtime prevention**, not observability.

Also not a LangGraph fork — you keep your graph; wrap side-effect tools only.

---

## What else Mycelium covers (v1.2)

We're still cataloging agent failures from GitHub issues and shipping guards incrementally. This release also includes:

- **Stale / broken context** — TTL cache, message repair, history limits
- **Tool boundaries** — validate inputs/outputs, allowlists, scoped paths

This article focuses on the highest-cost bug: **duplicate side effects on retry.**

---

## Links

- **GitHub:** https://github.com/mycelium-labs/mycelium
- **PyPI:** https://pypi.org/project/mycelium-runtime/
- **Issue:** https://github.com/langchain-ai/langgraph/issues/7417
- **Handbook:** https://mycelium-labs.github.io/mycelium/

```bash
pip install mycelium-runtime
mycelium demo
```

---

**Question for readers:** What side-effect tools are you running in production (payments, email, trades, subagents)? What failure mode should we tackle next?

---

*Mycelium is MIT licensed. Python 3.10+.*
