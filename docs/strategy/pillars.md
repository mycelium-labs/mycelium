# Mycelium — Product pillars

> The five decisions that define Mycelium. Everything the company ships, sells,
> or writes should be traceable back to one of these. If a feature, deck slide,
> or sales call doesn't serve a pillar, it's off-strategy.

Status: v0, locked 2026-04-25. Revisit after first paying design partner.

---

## 1. Framework-agnostic runtime

**What:** Mycelium wraps any agent stack without requiring a rewrite.
Supported surfaces (v1 scope): LangGraph, CrewAI, AutoGen, OpenAI Agents SDK,
Anthropic SDK, raw model-provider loops. Adapters are thin — the runtime core
is independent of any framework.

**Why it matters:** Enterprises don't standardize on one agent framework. Every
team picks what fits the job. A product that requires them to switch stacks
loses the deal at the architecture-review stage. Mycelium says yes to whatever
they already built.

**What it is NOT:** A new orchestration framework. We do not replace LangGraph
or compete with Temporal. We sit under the tool-calling loop of whatever they
already chose.

**Implication for roadmap:** Adapter breadth is a feature the market will pay
for. First adapter is picked from M1 data (whichever framework has the highest
failure-report volume in the corpus).

---

## 2. Compliance-grade by default

**What:** Audit trail on every tool call, cryptographic provenance on every
factual claim, RBAC, per-tenant isolation, data-residency controls, and a
defensible answer to every item on a SOC 2 / ISO 27001 / HIPAA / DPDP review.

**Why it matters:** In regulated industries (fintech, healthcare, legal,
government, pharma) agents are effectively banned in production today — not
because the tech doesn't work, but because the vendors can't answer the
compliance questions. Mycelium's default answer is "yes, already handled."
That's the wedge.

**What it is NOT:** A prompt-injection-only product (that's a subset). Not a
guardrails-as-a-service DLP layer. Compliance here is architectural — it's
about making consequences traceable, not about filtering text.

**Implication for roadmap:** Every feature must ship with its audit-log
shape defined. Every protection module emits provenance records.
Compliance is not a future enterprise-tier bolt-on; it is built in at v1.

---

## 3. Prevention, not observation

**What:** Mycelium sits under the tool-calling loop and refuses to let the
next step happen until preconditions are mechanically verified. Bad actions
are **blocked before they commit**, not flagged after.

**Why it matters:** This is the sharpest differentiator vs the observability
stack (LangSmith, Langfuse, Arize, Braintrust). Observability tells you what
broke *after* it broke. Mycelium stops it from breaking. This is the one-line
pitch that makes a CISO lean forward.

Observability and prevention are complements, not competitors. Customers will
run both. But prevention is the harder, stickier, higher-leverage layer —
and nobody else is building it.

**What it is NOT:** Not an eval platform. Evals run pre-deployment; Mycelium
runs at runtime. Not a trace viewer; it emits traces but is not a dashboard
product.

**Implication for roadmap:** Every protection module must have an enforcement
path, not just a detection path. A feature that only *reports* a failure is
incomplete.

---

## 4. Proprietary failure-mode catalog (the moat)

**What:** AF-001 through AF-00N (and growing): a structured, versioned catalog
of the ways autonomous agents fail, mapped to concrete runtime fixes. Grown
from public sources (GitHub issues, incident databases, benchmarks, press),
from customer telemetry (opt-in), and from internal threat research.

**Why it matters:** Every customer's caught failure enriches the catalog.
Every other customer is protected before they hit the same thing. Same
network-effect data moat as Crowdstrike's attack telemetry or Sentry's error
corpus. The SDK is the distribution; the **catalog is the company**.

**What it is NOT:** A taxonomy doc. It's a living, versioned, machine-readable
corpus that drives code generation, policy defaults, and protection modules.

**Implication for roadmap:** Corpus work (ingestion, tagging, research) is
not support work — it's the product. The dogfooding and scraping pipelines
running today are P0 infra, not side projects.

---

## 5. Self-hostable, data-sovereign

**What:** The SDK runs in-process — trace data never leaves the customer's
runtime by default. For enterprises that want a richer UX, an optional
**gateway** mode ships as a single container deployed in their VPC. The
Mycelium cloud control plane handles only policy metadata and aggregated
signals — **never** raw trace payloads.

**Why it matters:** Regulated enterprises can't send tool-call traces to a
third-party SaaS. Most AI startups don't have this option because their
products are centralized. Data sovereignty is a binary procurement filter:
either you have it or you lose the deal.

**What it is NOT:** An on-prem-only product. The cloud control plane is real
and valuable — policy management, catalog sync, aggregate telemetry,
benchmarking. But the trace data path is severable, and that's what matters.

**Implication for roadmap:** Every cloud feature must have an air-gapped
fallback. The gateway container is as much a first-class deliverable as the
SDK.

---

## The strategic fork: compliance as pillar vs. ICP

"Deep compliance" is actually two decisions that compound but should be
named separately:

| Decision | Meaning | Trade-off |
|---|---|---|
| **Compliance as product pillar** (pillar 2) | Every customer gets SOC 2, encryption, audit, RBAC — built in. | Table stakes for any B2B enterprise play. |
| **Compliance-heavy as first ICP** | Fintech, healthcare, legal, gov are the first 10 customers. | Longer sales cycles, much higher ACV, sticker stickiness, brutal procurement. |

Mycelium is currently saying yes to both. That's the right call because they
reinforce each other — and because non-regulated customers benefit from the
same primitives at no extra cost.

**Consequence for design-partner outreach (M5):** the first 20-person outreach
list should over-index on regulated enterprises. Not "any company running
agents." That would be a different company.

---

## What is NOT a pillar (anti-goals)

Written down explicitly so they don't drift in later under the pressure of a
shiny demo or a customer request:

- **Not an orchestration framework.** Agents still run their loop.
- **Not an observability product.** We emit traces; we are not LangSmith.
- **Not a prompt-security-only company.** That's one module of many.
- **Not an eval platform.** Evals are pre-deployment; we are runtime.
- **Not open source.** The catalog, protection engine, and control plane are
  closed. The taxonomy names and benchmark harness are public for marketing.
- **Not a research organization.** The catalog serves the product; the product
  does not serve the research.

---

## When to revisit this doc

- After the first paying design partner closes (pillars may sharpen).
- If a pillar is in conflict with a $1M+ deal — re-examine the pillar, don't
  abandon it quietly.
- Annually, at minimum. File a dated diff in `docs/strategy/pillars-v{N}.md`
  rather than overwriting. History matters.
