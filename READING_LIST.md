# Mycelium Reading List

Resources for Mycelium's current scope, planned scope, and adjacent companies/startups to watch.

Mycelium's core thesis: production agents need explicit runtime guards around context, tools, retries, and side effects. These resources are grouped by the failure boundary they help explain.

## Domain-Specific Scope

- [ ] Memory for customer support agents.
- [ ] Guardrails for finance agents.
- [ ] Prompt-injection defense for enterprise RAG apps.
- [ ] Audit logs for healthcare AI assistants.
- [ ] Safe tool-calling for internal developer agents.
- [ ] Context management for coding agents.
- [ ] Privacy-preserving memory for personal assistants.
- [ ] AI governance, data privacy, secure retrieval, access control, compliance, logging, agent permissions, human-in-the-loop review, model evaluation.

## Start Here

- [ ] [langgraph#7417: Long tool calls silently re-executed from checkpoint](https://github.com/langchain-ai/langgraph/issues/7417) - core duplicate side-effect / redispatch bug.
- [ ] [crewAI#5802: Tool re-execution on task retry](https://github.com/crewAIInc/crewAI/issues/5802) - same retry-induced duplication failure class in CrewAI.
- [ ] [crewAI PR #5822: idempotency guard](https://github.com/crewAIInc/crewAI/pull/5822) - useful design comparison for pre-claim and in-memory dedupe gaps.
- [ ] [Polaris Protocol](https://polaris-protocol.org) - commit-gated execution: side effects may occur only as a consequence of a validated, committed canonical state transition.
- [ ] [Polaris Protocol specification](https://github.com/polaris-specs/polaris-protocol) - normative schemas and reference material for canonical progression, validation preconditions, and execution causality binding.
- [ ] [multi-agent-failure-patterns](https://github.com/samueltradingpro1216-ops/multi-agent-failure-patterns) - catalogue of production multi-agent failure patterns.

## Idempotency And Side Effects

- [ ] [Stripe: Idempotent requests](https://docs.stripe.com/api/idempotent_requests) - practical reference for retry-safe writes.
- [ ] [Stripe: Advanced error handling](https://docs.stripe.com/error-low-level) - unknown outcomes, retries, and idempotency keys.
- [ ] [Backend Storage Patterns for Idempotency](https://www.distributedrequest.com/backend-implementation-storage-patterns/) - durable storage patterns for duplicate prevention.
- [ ] [PostgreSQL Unique Constraints vs Application-Level Idempotency Checks](https://www.distributedrequest.com/backend-implementation-storage-patterns/database-unique-constraints-upserts/postgresql-unique-constraints-vs-application-level-checks/) - race conditions, `ON CONFLICT`, and TOCTOU gaps.

## Tool Boundaries And Guardrails

- [ ] [OpenAI: Guardrails and human review](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals) - input, output, tool, and approval guardrails.
- [ ] [Tool calling reliability: schemas, validation, retries](https://aivineet.com/tool-calling-reliability-llm-agents-schemas-validation-retries/) - practical guide aligned with `@bounded`, `ToolRunner`, and idempotency.
- [ ] [Shipping Safe Tooling for Tool Calling Agents](https://fieldjournal.ai/blog/shipping-safe-tooling-for-tool-calling-agents/) - tool schemas, retry chaos, and untrusted tool output.
- [ ] [Tool Calling Is Where Agents Break](https://dev.to/sagar_jain4010/tool-calling-is-where-agents-break-a-reliability-guide-5f8g) - simple framing for why tool calls are the dangerous boundary.

## Stale Context And Freshness

- [ ] [Temporal Validity in Retrieval Memory](https://arxiv.org/html/2606.26511) - stale-fact errors and temporal memory.
- [ ] [RAG That Doesn't Rot](https://appropri8.com/blog/2026/01/05/rag-freshness-aware-retrieval/) - freshness-aware retrieval, stale citation rate, and incremental indexing.
- [ ] [Data freshness rot in production RAG](https://glenrhodes.com/data-freshness-rot-as-the-silent-failure-mode-in-production-rag-systems-and-treating-document-shelf-life-as-a-first-class-reliability-concern/) - freshness as a reliability concern.
- [ ] [RAG Is Blind to Time](https://towardsdatascience.com/rag-is-blind-to-time-i-built-a-temporal-layer-to-fix-it-in-production/) - temporal reranking and current-vs-old knowledge.

## History And Memory Failures

- [ ] [Lost in the Middle](https://doi.org/10.1162/tacl_a_00638) - why long context is not reliable memory.
- [ ] [Lost in the Middle PDF](https://aclanthology.org/2024.tacl-1.9.pdf) - direct paper PDF.
- [ ] [Governing Evolving Memory in LLM Agents](https://doi.org/10.48550/arxiv.2603.11768) - memory corruption, drift, privacy leakage, and governed memory.
- [ ] [Agent Memory Paper List](https://github.com/Shichun-Liu/Agent-Memory-Paper-List) - running bibliography for agent memory research.
- [ ] [MemGPT / virtual context management concepts](https://github.com/deveshraj/ai-engineering-from-scratch-course/blob/main/phases/14-agent-engineering/07-memory-virtual-context-memgpt/docs/en.md) - main context as RAM, external memory as disk.
- [ ] [Agent memory paper: arXiv 2512.13564](https://arxiv.org/abs/2512.13564) - memory/context research to review.
- [ ] [Agent memory paper: arXiv 2501.13956](https://arxiv.org/abs/2501.13956) - memory/context research to review.
- [ ] [Agent memory paper: arXiv 2501.00309](https://arxiv.org/abs/2501.00309) - memory/context research to review.

## Graph Memory And GraphRAG

- [ ] [Microsoft GraphRAG](https://microsoft.github.io/graphrag/) - graph-based retrieval architecture and implementation docs.
- [ ] [A Survey of GraphRAG for Customized LLMs](https://arxiv.org/abs/2501.13958) - survey of graph retrieval for LLM systems.
- [ ] [Awesome-GraphRAG](https://github.com/DEEP-PolyU/Awesome-GraphRAG) - curated GraphRAG resources.
- [ ] [GraphRAG paper: arXiv 2601.13969](https://arxiv.org/pdf/2601.13969) - GraphRAG research to review.
- [ ] [GraphRAG paper: arXiv 2509.10852](https://arxiv.org/pdf/2509.10852) - GraphRAG research to review.

## Context Engineering

- [ ] [Context Engineering: A Framework for Robust Generative AI Systems](https://www.sundeepteki.org/blog/context-engineering-a-framework-for-robust-generative-ai-systems) - RAG, memory, retrieval/filtering, compression, isolation, and GraphRAG.
- [ ] [LangGraph long-term memory docs](https://docs.langchain.com/oss/python/langchain/long-term-memory) - stores, namespaces, keys, and cross-session memory patterns.

## Multi-Agent Failure Modes

- [ ] [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/html/2503.13657v1) - taxonomy of specification, coordination, and verification failures.
- [ ] [LLM FAIL?](https://openreview.net/pdf?id=wM521FqPvI) - multi-agent failure taxonomy.
- [ ] [MAS-FIRE](https://www.arxiv.org/pdf/2602.19843) - fault injection for planning, memory, reasoning, and action faults.
- [ ] [Self-Healing Agentic Orchestrators](https://arxiv.org/html/2606.01416v1) - recovery policies, failure signals, and verification after repair.

## Agent Security

- [ ] [OWASP Top 10 for Agentic Applications](https://www.promptfoo.dev/docs/red-team/owasp-agentic-ai/) - goal hijack, tool misuse, memory poisoning, cascading failures.
- [ ] [OWASP GenAI Prompt Injection](https://github.com/GenAI-Security-Project/GenAI-LLM-Top10/blob/main/2026/LLM01_PromptInjection.md) - prompt injection across user input, retrieved docs, tool output, and memory.
- [ ] [OWASP LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html) - practical prompt-injection defense patterns.
- [ ] [LLM Security Guide](https://github.com/requie/LLMSecurityGuide) - OWASP GenAI risks and LLM security resources.
- [ ] [Datadog: LLM guardrails best practices](https://www.datadoghq.com/blog/llm-guardrails-best-practices/) - production guardrail patterns and monitoring.
- [ ] [Demystifying OWASP Top 10 Agentic Applications](https://idanhabler.medium.com/demystifying-the-owasp-top-10-for-agentic-applications-4eedba941b2c) - readable guide to agentic security risks.
- [ ] [AI Agent Guardrails Checklist](https://looprails.dev/article-ai-agent-guardrails.html) - capability locks, blast radius, sandboxing, and approvals.
- [ ] [Prompt-injection benchmark for RAG-enabled agents](https://arxiv.org/html/2511.15759v1) - direct injection, context manipulation, instruction override, data exfiltration, and cross-context contamination.

## Agent Experience And Agent-Operable Products

- [ ] [Salesforce: Agent Experience Design](https://www.salesforce.com/blog/agent-experience-design/) - product design for agent interactions.
- [ ] [Microsoft: Design for Agents](https://learn.microsoft.com/en-us/agents/design-guidelines/overview) - design guidelines for agentic applications.
- [ ] [Agent Experience patterns](https://agent-experience.dev/) - patterns for agent-facing product surfaces.
- [ ] [Designing for Intelligence: APIs, Databases, and Interfaces](https://stevenyue.com/blogs/designing-for-intelligence-rethinking-apis-databases-and-interfaces/) - API and product design for AI agents.
- [ ] [Designing APIs for AI Agents](https://berris.dev/blogs/designing-apis-for-ai-agents) - agent-operable API design.
- [ ] [From Human Users to AI Agents](https://dev.to/bridgeai/from-human-users-to-ai-agents-rethinking-product-interfaces-for-the-next-era-of-web-traffic-3ene) - autonomous agent UX controls.
- [ ] [Agentic UX: Designing Interfaces for Agents](https://standardbeagle.com/agentic-ux-designing-interfaces-for-agents/) - UX patterns for agent-facing systems.
- [ ] [UI Design for AI Agents](https://fuselabcreative.com/ui-design-for-ai-agents/) - interface design considerations for agent workflows.

## Observability And Aggregate Health

- [ ] [Langfuse](https://langfuse.com/?tab=evaluation) - open-source tracing, evals, prompt management, and datasets.
- [ ] [OpenTelemetry for LLM Observability](https://langfuse.com/blog/2024-10-opentelemetry-for-llm-observability) - vendor-neutral traces and GenAI observability.
- [ ] [Top LLM Observability Platforms](https://agenta.ai/blog/top-llm-observability-platforms) - observability platform landscape.
- [ ] [Best LLM Observability Tools](https://www.firecrawl.dev/blog/best-llm-observability-tools) - practical market overview.

## Evaluation And Replay Sets

- [ ] [AgentBench](https://ar5iv.labs.arxiv.org/html/2308.03688) - agent benchmark across OS, DB, web, games, and more.
- [ ] [AgentBench GitHub](https://github.com/THUDM/AgentBench/) - benchmark source.
- [ ] [tau-bench](https://github.com/sierra-research/tau-bench) - tool-agent-user interactions verified against end state.
- [ ] [A Survey on Evaluation of LLM-based Agents](https://aclanthology.org/2026.findings-acl.1330.pdf) - broad survey of agent evaluation.
- [ ] [AI Agent Evaluation: Metrics, Frameworks, Production Failures](https://www.morphllm.com/ai-agent-evaluation) - practical overview of trajectory evals and production failures.

## Foundational Agent And Tool-Use Papers

- [ ] [ReAct: Synergizing Reasoning and Acting](https://arxiv.org/abs/2210.03629) - foundational reasoning-plus-acting loop.
- [ ] [Toolformer](https://proceedings.neurips.cc/paper_files/paper/2023/file/d842425e4bf79ba039352da0f658a906-Paper-Conference.pdf) - self-supervised tool use.
- [ ] [Agentic Tool Use in Large Language Models](https://arxiv.org/pdf/2604.00835v1) - broad survey of tool-use paradigms.
- [ ] [The Evolution of Tool Use in LLM Agents](https://doi.org/10.48550/arxiv.2603.22862) - from single tool calls to multi-tool orchestration.

## Production Agent Reliability

- [ ] [Why Agent Loops Fail in Production](https://www.cockroachlabs.com/blog/agent-loops-production-database-patterns/) - state, retries, crashes, stale memory, and partial writes.
- [ ] [What Breaks When Agentic AI Reaches Production?](https://www.cockroachlabs.com/blog/agentic-ai-production-infrastructure/) - durable state, identity, observability, and scale.
- [ ] [Designing Agentic Systems That Don't Collapse in Production](https://koder.ai/blog/designing-agentic-systems-that-dont-collapse-production) - state machines, tool contracts, retries, and observability.
- [ ] [What We Learned Deploying AI Agents in Production for 12 Months](https://viqus.ai/blog/ai-agents-production-lessons-2026) - production lessons around memory, retries, evals, and human fallback.

## Companies And Startups To Watch

### Closest To Mycelium

- [ ] [LangChain / LangGraph / LangSmith](https://www.langchain.com/) - framework, stateful orchestration, and observability.
- [ ] [CrewAI](https://www.crewai.com/) - multi-agent framework with relevant duplicate-tool execution failure reports.
- [ ] [Temporal](https://temporal.io/) - durable execution, workflow replay, retries, and side-effect safety.
- [ ] [Inngest](https://www.inngest.com/) - durable background jobs, retries, and idempotent workflows.
- [ ] [MartinLoop](https://martinloop.com) - safety layer around coding agents with budgets, stop rules, verifiers, rollback paths, and run receipts.
- [ ] [Pydantic AI](https://ai.pydantic.dev/) - type-safe agents, tools, and structured outputs.

### Observability And Evals

- [ ] [Langfuse](https://langfuse.com/) - open-source tracing, evals, prompt management, and datasets.
- [ ] [Braintrust](https://www.braintrust.dev/) - eval-first AI engineering and regression blocking.
- [ ] [Arize Phoenix](https://phoenix.arize.com/) - OpenTelemetry-native observability and evals.
- [ ] [Helicone](https://www.helicone.ai/) - LLM proxy observability, cost tracking, and caching.
- [ ] [Latitude](https://latitude.so/) - production agent observability and eval workflows.
- [ ] [AgentOps](https://www.agentops.ai/) - agent tracing and monitoring.

### Guardrails And Security

- [ ] [Promptfoo](https://www.promptfoo.dev/) - open-source evals and red teaming.
- [ ] [Lakera](https://www.lakera.ai/) - LLM firewall and red teaming.
- [ ] [HiddenLayer](https://www.hiddenlayer.com/) - AI runtime security and agentic threat detection.
- [ ] [Protect AI](https://protectai.com/) - AI and ML security platform.
- [ ] [CalypsoAI](https://calypsoai.com/) - enterprise AI security and governance.
- [ ] [Cisco AI Defense / Robust Intelligence](https://www.robustintelligence.com/) - AI firewall, red teaming, and runtime protection.
- [ ] [Zenity](https://www.zenity.io/) - agentic runtime security and action governance.

### Agent Memory And Context

- [ ] [Letta](https://www.letta.com/) - stateful agents with memory, from the MemGPT lineage.
- [ ] [Mem0](https://mem0.ai/) - agent memory layer for personalization and long-term recall.
- [ ] [Zep](https://www.getzep.com/) - temporal knowledge graph memory.
- [ ] [Supermemory](https://supermemory.ai/) - memory API for stateful agents.
- [ ] [Pinecone](https://www.pinecone.io/) - vector database for retrieval systems.
- [ ] [Weaviate](https://weaviate.io/) - vector database and hybrid search.
- [ ] [LlamaIndex](https://www.llamaindex.ai/) - data/RAG framework for retrieval-centric agents.

### Agent Frameworks And Runtime

- [ ] [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) - agent runtime with tools, handoffs, and guardrails.
- [ ] [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) - enterprise agent framework.
- [ ] [AutoGen / AG2](https://github.com/ag2ai/ag2) - multi-agent framework lineage.
- [ ] [Vercel AI SDK](https://sdk.vercel.ai/) - TypeScript SDK for AI apps and agents.
- [ ] [Mastra](https://mastra.ai/) - TypeScript agent framework.
- [ ] [Agno](https://www.agno.com/) - lightweight production agent framework and runtime.

