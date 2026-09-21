"""Built-in ``mycelium doctor`` checks (read-only; no tool/LLM execution)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from mycelium.action_ledger import LEDGER_ENTRY_SCHEMA_VERSION
from mycelium.config import (
    PROFILE_PRODUCTION,
    MyceliumConfig,
    _missing_run_id_policy,
    _missing_usage_policy,
    _outcome_on_failure,
    _request_identity_policy,
)
from mycelium.destructive_confirm import (
    SHARED_GRANT_STORAGES,
    SINGLE_NODE_GRANT_STORAGES,
    STORAGE_MEMORY,
    registered_destructive_canonicalizers,
)
from mycelium.doctor.connectivity import (
    host_hint_from_url,
    probe_file_path,
    probe_postgres,
    probe_redis,
    probe_sqlite_path,
    safe_backend_label,
)
from mycelium.doctor.registry import DoctorContext, doctor_check
from mycelium.doctor.types import (
    EVIDENCE_CONNECTIVITY,
    EVIDENCE_NOT_VERIFIABLE,
    EVIDENCE_OPERATOR,
    EVIDENCE_RUNTIME,
    EVIDENCE_STATIC,
    DoctorCheck,
    DoctorStatus,
)
from mycelium.ledger_migrations import inspect_ledger_schema_versions
from mycelium.loop_guard import MISSING_RUN_ID_POLICY_ERROR
from mycelium.outcome_emit import (
    OUTCOME_ON_FAILURE_ERROR,
    FileOutcomeStorage,
    InMemoryOutcomeStorage,
    OutcomeStorage,
)
from mycelium.storage._helpers import redact_secrets, resolve_storage_url
from mycelium.storage.postgres_outcome import PostgresOutcomeStorage
from mycelium.storage.redis_outcome import RedisOutcomeStorage
from mycelium.transition import (
    CONSEQUENTIAL_SIDE_EFFECT_CLASSES,
    REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT,
    SideEffectClass,
)

SINGLE_NODE_STORAGES = frozenset({"file", "sqlite", "memory"})
DISTRIBUTED_STORAGES = frozenset({"postgres", "redis"})


def _check(
    *,
    id: str,
    category: str,
    status: DoctorStatus,
    summary: str,
    details: str = "",
    remediation: str = "",
    evidence: str = EVIDENCE_STATIC,
    blocking: bool = True,
) -> DoctorCheck:
    return DoctorCheck(
        id=id,
        category=category,
        status=status,
        summary=summary,
        details=details,
        remediation=remediation,
        evidence=evidence,
        blocking=blocking,
    )


def _ledger_storage_for_tool(cfg: MyceliumConfig, tool_name: str) -> dict[str, Any]:
    tool = cfg.tools[tool_name]
    if tool.ledger:
        return dict(tool.ledger)
    if cfg.action_ledger:
        return dict(cfg.action_ledger)
    return {"storage": "memory"}


def _consequential_tools(cfg: MyceliumConfig) -> list[str]:
    names: list[str] = []
    for name, tool in cfg.tools.items():
        if tool.side_effect_class in CONSEQUENTIAL_SIDE_EFFECT_CLASSES:
            names.append(name)
    return names


def _storage_type(raw: dict[str, Any] | None) -> str:
    if not raw:
        return "memory"
    return str(raw.get("storage", "memory"))


def _guard_storage_type(
    cfg: MyceliumConfig,
    raw: dict[str, Any] | None,
) -> str:
    if raw is None:
        return "memory"
    storage = raw.get("storage")
    if storage == "shared" or (storage is None and cfg.state_backend is not None):
        return _storage_type(cfg.state_backend)
    return str(storage or "memory")


@doctor_check("configuration")
def check_configuration(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    cfg = ctx.config
    contract_count = sum(1 for tool in cfg.tools.values() if tool.contract is not None)
    details = (
        f"profile={cfg.profile!r}; tools={len(cfg.tools)}; "
        f"contracts={contract_count}; langgraph_enabled={cfg.langgraph_enabled}; "
        f"crewai_enabled={cfg.crewai_enabled}"
    )
    if cfg.profile == PROFILE_PRODUCTION:
        yield _check(
            id="configuration.profile",
            category="Configuration",
            status=DoctorStatus.PASS,
            summary="Production profile loaded",
            details=details,
            evidence=EVIDENCE_STATIC,
        )
    else:
        yield _check(
            id="configuration.profile",
            category="Configuration",
            status=DoctorStatus.WARN,
            summary="Development profile loaded",
            details=details,
            remediation=(
                "Set profile: production for fail-closed defaults "
                "(request identity, durable outcomes, memory ledger policy)."
            ),
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )


@doctor_check("tool_classification")
def check_tool_classification(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    cfg = ctx.config
    if not cfg.tools:
        yield _check(
            id="tools.none",
            category="Tool classification",
            status=DoctorStatus.SKIP,
            summary="No tools configured",
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        return

    unclassified: list[str] = []
    lines: list[str] = []
    for name, tool in sorted(cfg.tools.items()):
        sec = tool.side_effect_class
        consequential = sec in CONSEQUENTIAL_SIDE_EFFECT_CLASSES if sec else False
        storage = _storage_type(_ledger_storage_for_tool(cfg, name))
        identity = (
            f"request_id_from={tool.request_id_from!r}"
            if tool.request_id_from
            else (
                "explicit request_id required"
                if cfg.profile == PROFILE_PRODUCTION
                and consequential
                and _request_identity_policy(cfg.action_ledger, profile=cfg.profile)
                == REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT
                else "derived/host"
            )
        )
        if sec is None:
            unclassified.append(name)
            cls_label = "unclassified"
        else:
            cls_label = sec.value
        lines.append(
            f"{name}: class={cls_label} consequential={consequential} "
            f"ledger={storage} identity={identity}"
        )

    policy = "warn"
    if cfg.action_ledger:
        policy = str(cfg.action_ledger.get("unclassified_policy", "warn"))

    if unclassified:
        yield _check(
            id="tools.unclassified",
            category="Tool classification",
            status=DoctorStatus.WARN,
            summary=f"{len(unclassified)} tool(s) are unclassified",
            details="; ".join(lines),
            remediation=(
                "Declare side_effect_class on important tools. "
                f"unclassified_policy={policy!r} is an accepted product choice "
                "(warn remains allowed); consequential tools should still be classified."
            ),
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
    else:
        yield _check(
            id="tools.classified",
            category="Tool classification",
            status=DoctorStatus.PASS,
            summary=f"{len(cfg.tools)} tool(s) report side-effect classes",
            details="; ".join(lines),
            evidence=EVIDENCE_STATIC,
        )


@doctor_check("request_identity")
def check_request_identity(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    cfg = ctx.config
    policy = _request_identity_policy(cfg.action_ledger, profile=cfg.profile)
    consequential = _consequential_tools(cfg)

    if cfg.profile == PROFILE_PRODUCTION:
        if policy != REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT:
            yield _check(
                id="identity.policy",
                category="Request identity",
                status=DoctorStatus.FAIL,
                summary="Production request identity policy is not require_explicit",
                details=f"resolved policy={policy!r}",
                remediation=(
                    "Set action_ledger.request_identity_policy: require_explicit "
                    "(or omit it under profile: production)."
                ),
                evidence=EVIDENCE_STATIC,
            )
            return
        if not consequential:
            yield _check(
                id="identity.policy",
                category="Request identity",
                status=DoctorStatus.PASS,
                summary="require_explicit active (no consequential tools)",
                details="policy=require_explicit",
                evidence=EVIDENCE_STATIC,
            )
            return

        missing_from: list[str] = []
        configured_from: list[str] = []
        for name in consequential:
            tool = cfg.tools[name]
            if tool.request_id_from:
                configured_from.append(f"{name}:{tool.request_id_from}")
            else:
                # Host must pass request_id at call time — static policy proves
                # derived/tool_call_id fallback is rejected.
                missing_from.append(name)

        details = (
            f"policy=require_explicit; request_id_from={configured_from or 'none'}; "
            f"host request_id required for={missing_from or 'none'}. "
            "tool_call_id/run_id/thread_id are not accepted as business IDs."
        )
        yield _check(
            id="identity.policy",
            category="Request identity",
            status=DoctorStatus.PASS,
            summary=(
                f"{len(consequential)} consequential tool(s) require business IDs"
            ),
            details=details,
            evidence=EVIDENCE_STATIC,
        )
        if missing_from:
            yield _check(
                id="identity.request_id_from_hint",
                category="Request identity",
                status=DoctorStatus.WARN,
                summary=(
                    f"{len(missing_from)} consequential tool(s) rely on host "
                    "request_id (no request_id_from)"
                ),
                details=", ".join(missing_from),
                remediation=(
                    "Optionally set tools.<name>.request_id_from to a "
                    "server-owned argument (e.g. order_id), or ensure every "
                    "call site passes an explicit host-owned request_id. "
                    "Doctor cannot verify call sites."
                ),
                evidence=EVIDENCE_NOT_VERIFIABLE,
                blocking=False,
            )
        return

    yield _check(
        id="identity.policy",
        category="Request identity",
        status=DoctorStatus.WARN,
        summary=f"Request identity policy is {policy!r}",
        details=(
            "Development may use derived identities. Production requires "
            "require_explicit for consequential tools."
        ),
        remediation="Set profile: production to enforce host-owned business IDs.",
        evidence=EVIDENCE_STATIC,
        blocking=False,
    )


def _probe_storage(
    ctx: DoctorContext,
    *,
    raw: dict[str, Any],
    label: str,
) -> DoctorCheck | None:
    if not ctx.connectivity:
        return _check(
            id=f"{label}.connectivity",
            category="Connectivity",
            status=DoctorStatus.SKIP,
            summary=f"{label} connectivity skipped (--no-connectivity)",
            evidence=EVIDENCE_NOT_VERIFIABLE,
            blocking=False,
        )
    storage = _storage_type(raw)
    try:
        if storage == "postgres":
            dsn = resolve_storage_url(raw, url_key="url", alt_keys=("dsn",))
            result = probe_postgres(dsn, timeout_seconds=ctx.timeout_seconds)
            hint = host_hint_from_url(dsn)
        elif storage == "redis":
            url = resolve_storage_url(raw, url_key="url")
            result = probe_redis(url, timeout_seconds=ctx.timeout_seconds)
            hint = host_hint_from_url(url)
        elif storage == "sqlite":
            path = str(raw.get("path") or "")
            result = probe_sqlite_path(path)
            hint = path
        elif storage == "file":
            path = str(raw.get("path") or "")
            result = probe_file_path(path)
            hint = path
        else:
            return None
    except Exception as exc:
        return _check(
            id=f"{label}.connectivity",
            category="Connectivity",
            status=DoctorStatus.FAIL,
            summary=f"{label} connectivity probe could not start",
            details=redact_secrets(str(exc)),
            remediation="Fix connection settings (url/url_env/dsn/dsn_env/path).",
            evidence=EVIDENCE_CONNECTIVITY,
        )

    if result.ok:
        return _check(
            id=f"{label}.connectivity",
            category="Connectivity",
            status=DoctorStatus.PASS,
            summary=f"{label} connectivity ok ({hint})",
            details=result.message,
            evidence=EVIDENCE_CONNECTIVITY,
        )
    return _check(
        id=f"{label}.connectivity",
        category="Connectivity",
        status=DoctorStatus.FAIL,
        summary=f"{label} connectivity {result.kind}",
        details=result.message,
        remediation=(
            "Restore backend reachability/credentials/permissions. "
            "Doctor does not write test rows."
        ),
        evidence=EVIDENCE_CONNECTIVITY,
    )


@doctor_check("action_ledger")
def check_action_ledger(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    cfg = ctx.config
    consequential = _consequential_tools(cfg)
    if not consequential:
        yield _check(
            id="ledger.skipped",
            category="Action ledger",
            status=DoctorStatus.SKIP,
            summary="No consequential tools; ledger durability not required",
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        return

    # Group by storage type across consequential tools.
    by_storage: dict[str, list[str]] = {}
    for name in consequential:
        storage = _storage_type(_ledger_storage_for_tool(cfg, name))
        by_storage.setdefault(storage, []).append(name)

    fails: list[str] = []
    warns: list[str] = []
    for storage, tools in sorted(by_storage.items()):
        if storage == "memory":
            if cfg.profile == PROFILE_PRODUCTION:
                fails.append(
                    f"memory ledger for consequential tools {tools} "
                    "(rejected in production)"
                )
            else:
                warns.append(f"memory ledger for {tools} (process-local only)")
        elif storage in ("file", "sqlite"):
            warns.append(
                f"{storage} ledger for {tools} is durable but single-node only"
            )
        elif storage in DISTRIBUTED_STORAGES:
            pass
        else:
            fails.append(f"unknown ledger storage {storage!r} for {tools}")

    sample = _ledger_storage_for_tool(cfg, consequential[0])
    label = safe_backend_label(sample)

    if fails:
        yield _check(
            id="ledger.backend",
            category="Action ledger",
            status=DoctorStatus.FAIL,
            summary="ActionLedger storage is not production-safe",
            details="; ".join(fails),
            remediation=(
                "Use action_ledger.storage: postgres (recommended) or redis "
                "for multi-node; sqlite/file only for single-node; never memory "
                "for consequential tools in production."
            ),
            evidence=EVIDENCE_STATIC,
        )
        return

    if any(s in DISTRIBUTED_STORAGES for s in by_storage):
        summary = f"Shared/distributed ledger backend configured ({label})"
        status = DoctorStatus.PASS
    else:
        summary = f"Single-node ledger backend configured ({label})"
        status = DoctorStatus.PASS

    details = "; ".join(
        f"{storage}: {tools}" for storage, tools in sorted(by_storage.items())
    )
    if warns:
        details = details + " | " + "; ".join(warns)

    yield _check(
        id="ledger.backend",
        category="Action ledger",
        status=status,
        summary=summary,
        details=details,
        remediation=(
            "Prefer postgres for multi-node deployments. File/SQLite are "
            "single-node only."
            if warns
            else ""
        ),
        evidence=EVIDENCE_STATIC,
    )

    for warn in warns:
        if "single-node" in warn:
            yield _check(
                id="ledger.single_node",
                category="Action ledger",
                status=DoctorStatus.WARN,
                summary="Ledger backend is single-node only",
                details=warn,
                remediation=(
                    "For multi-node workers use storage: postgres or redis. "
                    "Set deployment.topology: multi_node to make this a hard fail."
                ),
                evidence=EVIDENCE_STATIC,
                blocking=False,
            )
            break

    # Required fields for redis/postgres
    for storage, tools in by_storage.items():
        raw = _ledger_storage_for_tool(cfg, tools[0])
        if storage == "postgres":
            try:
                resolve_storage_url(raw, url_key="url", alt_keys=("dsn",))
            except ValueError as exc:
                yield _check(
                    id="ledger.postgres_config",
                    category="Action ledger",
                    status=DoctorStatus.FAIL,
                    summary="Postgres ledger config incomplete",
                    details=str(exc),
                    remediation="Set dsn/dsn_env or url/url_env for the ledger.",
                    evidence=EVIDENCE_STATIC,
                )
        if storage == "redis":
            try:
                resolve_storage_url(raw, url_key="url")
            except ValueError as exc:
                yield _check(
                    id="ledger.redis_config",
                    category="Action ledger",
                    status=DoctorStatus.FAIL,
                    summary="Redis ledger config incomplete",
                    details=str(exc),
                    remediation="Set url or url_env for the ledger.",
                    evidence=EVIDENCE_STATIC,
                )

    probe = _probe_storage(ctx, raw=sample, label="action_ledger")
    if probe is not None:
        yield probe

    durable_raws: list[dict[str, Any]] = []
    for name in consequential:
        candidate = _ledger_storage_for_tool(cfg, name)
        if _storage_type(candidate) != "memory" and candidate not in durable_raws:
            durable_raws.append(candidate)
    if not durable_raws:
        yield _check(
            id="ledger.schema",
            category="Action ledger",
            status=DoctorStatus.SKIP,
            summary="No durable ledger rows to inspect",
            evidence=EVIDENCE_NOT_VERIFIABLE,
            blocking=False,
        )
        return
    if not ctx.connectivity:
        yield _check(
            id="ledger.schema",
            category="Action ledger",
            status=DoctorStatus.SKIP,
            summary="Ledger schema inspection skipped (--no-connectivity)",
            remediation="Run mycelium doctor with connectivity enabled.",
            evidence=EVIDENCE_NOT_VERIFIABLE,
            blocking=False,
        )
        return
    if probe is not None and probe.status == DoctorStatus.FAIL:
        yield _check(
            id="ledger.schema",
            category="Action ledger",
            status=DoctorStatus.SKIP,
            summary="Ledger schema could not be inspected",
            details="The connectivity check failed first.",
            remediation="Restore backend connectivity, then run mycelium doctor again.",
            evidence=EVIDENCE_NOT_VERIFIABLE,
            blocking=False,
        )
        return

    versions: dict[int, int] = {}
    try:
        for raw in durable_raws:
            for version, count in inspect_ledger_schema_versions(raw).items():
                versions[version] = versions.get(version, 0) + count
    except Exception as exc:
        yield _check(
            id="ledger.schema",
            category="Action ledger",
            status=DoctorStatus.FAIL,
            summary="Ledger schema inspection failed",
            details=redact_secrets(str(exc)),
            remediation="Repair the malformed row or backend, then run migration planning.",
            evidence=EVIDENCE_CONNECTIVITY,
        )
        return

    version_detail = ", ".join(
        f"v{version}={count}" for version, count in sorted(versions.items())
    ) or "no rows"
    future = sorted(version for version in versions if version > LEDGER_ENTRY_SCHEMA_VERSION)
    older = sorted(version for version in versions if version < LEDGER_ENTRY_SCHEMA_VERSION)
    if future:
        yield _check(
            id="ledger.schema",
            category="Action ledger",
            status=DoctorStatus.FAIL,
            summary="Ledger contains unsupported future schema versions",
            details=version_detail,
            remediation="Upgrade Mycelium before reading or modifying these ledger rows.",
            evidence=EVIDENCE_CONNECTIVITY,
        )
    elif older:
        yield _check(
            id="ledger.schema",
            category="Action ledger",
            status=DoctorStatus.WARN,
            summary="Ledger migration is available",
            details=version_detail,
            remediation="Run 'mycelium migrate --plan', back up, then use --apply.",
            evidence=EVIDENCE_CONNECTIVITY,
            blocking=False,
        )
    else:
        yield _check(
            id="ledger.schema",
            category="Action ledger",
            status=DoctorStatus.PASS,
            summary=f"Ledger schema is current (v{LEDGER_ENTRY_SCHEMA_VERSION})",
            details=version_detail,
            evidence=EVIDENCE_CONNECTIVITY,
        )


@doctor_check("run_identity")
def check_run_identity(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    cfg = ctx.config
    guards: list[tuple[str, dict[str, Any]]] = []
    if cfg.loop_guard is not None:
        guards.append(("loop_guard", cfg.loop_guard))
    if cfg.scope_guard is not None:
        guards.append(("scope_guard", cfg.scope_guard))

    if not guards:
        yield _check(
            id="run_identity.skipped",
            category="Run identity",
            status=DoctorStatus.SKIP,
            summary="LoopGuard/ScopeGuard not enabled",
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        return

    failures: list[str] = []
    details: list[str] = []
    for name, raw in guards:
        policy = _missing_run_id_policy(
            raw, f"{name}.missing_run_id_policy", profile=cfg.profile
        )
        details.append(f"{name}.missing_run_id_policy={policy}")
        if cfg.profile == PROFILE_PRODUCTION and policy != MISSING_RUN_ID_POLICY_ERROR:
            failures.append(f"{name} policy is {policy!r}")

    if failures:
        yield _check(
            id="run_identity.policy",
            category="Run identity",
            status=DoctorStatus.FAIL,
            summary="Guards can skip when run_id is missing",
            details="; ".join(details + failures),
            remediation=(
                "Set missing_run_id_policy: error on loop_guard and scope_guard. "
                "thread_id alone is not sufficient under production policy."
            ),
            evidence=EVIDENCE_STATIC,
        )
        return

    # Can the selected integration supply run_id?
    if cfg.langgraph_enabled or cfg.crewai_enabled:
        integrations: list[str] = []
        if cfg.langgraph_enabled:
            integrations.append("integrations.langgraph.enabled=true")
        if cfg.crewai_enabled:
            integrations.append(
                "integrations.crewai.enabled=true"
                + (
                    f"; run_id_from={cfg.crewai_run_id_from!r}"
                    if cfg.crewai_run_id_from
                    else "; deterministic development run scope"
                )
            )
        yield _check(
            id="run_identity.policy",
            category="Run identity",
            status=DoctorStatus.PASS,
            summary="Missing run IDs fail closed",
            details=(
                "; ".join(details + integrations)
                + "; the host must still supply the selected stable run input."
            ),
            evidence=EVIDENCE_STATIC,
        )
        yield _check(
            id="run_identity.host_binding",
            category="Run identity",
            status=DoctorStatus.WARN,
            summary="Host must supply a stable run_id at runtime",
            details=(
                "Doctor cannot prove every framework invocation supplies its "
                "configured stable run identifier. thread_id alone is not "
                "accepted as a substitute under missing_run_id_policy: error."
            ),
            remediation=(
                "Ensure LangGraph configurable fields or the configured CrewAI "
                "kickoff input provide a host-owned run_id on every guarded call."
            ),
            evidence=EVIDENCE_NOT_VERIFIABLE,
            blocking=False,
        )
        return

    if cfg.profile == PROFILE_PRODUCTION:
        yield _check(
            id="run_identity.policy",
            category="Run identity",
            status=DoctorStatus.WARN,
            summary="Missing run IDs fail closed, but no integration selected",
            details="; ".join(details),
            remediation=(
                "Enable the matching LangGraph or CrewAI integration, "
                "or ensure your host always enters execution_scope with run_id."
            ),
            evidence=EVIDENCE_NOT_VERIFIABLE,
            blocking=False,
        )
    else:
        yield _check(
            id="run_identity.policy",
            category="Run identity",
            status=DoctorStatus.PASS,
            summary="Guard run-id policies resolved",
            details="; ".join(details),
            evidence=EVIDENCE_STATIC,
        )


@doctor_check("completion")
def check_completion(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    cfg = ctx.config
    if cfg.completion is None:
        yield _check(
            id="completion.skipped",
            category="Completion",
            status=DoctorStatus.SKIP,
            summary="completion: not configured",
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        return

    adapters = set(cfg._terminal_adapters)
    if not adapters:
        status = (
            DoctorStatus.FAIL
            if cfg.profile == PROFILE_PRODUCTION
            else DoctorStatus.WARN
        )
        yield _check(
            id="completion.adapter",
            category="Completion",
            status=status,
            summary="Completion configured but terminal adapter unwired",
            details=(
                f"langgraph_enabled={cfg.langgraph_enabled}; "
                f"crewai_enabled={cfg.crewai_enabled}; "
                "an importable framework alone is not enough"
            ),
            remediation=(
                "An importable framework is not enough. Set "
                "integrations.langgraph.enabled: true or "
                "integrations.crewai.enabled: true and install the matching "
                "extra, or register_terminal_adapter(...) before load_config()."
            ),
            evidence=EVIDENCE_RUNTIME,
        )
        return

    framework_adapters = {"langgraph", "crewai"}
    custom = adapters - framework_adapters
    evidence = EVIDENCE_RUNTIME
    details = f"adapters={sorted(adapters)}; checks run at verified graph END only"
    if "langgraph" in adapters and not cfg.langgraph_enabled:
        # Shouldn't happen — langgraph adapter requires enabled install path
        yield _check(
            id="completion.adapter",
            category="Completion",
            status=DoctorStatus.FAIL,
            summary="LangGraph terminal present without explicit selection",
            details=details,
            remediation="Set integrations.langgraph.enabled: true",
            evidence=EVIDENCE_RUNTIME,
        )
        return
    if "crewai" in adapters and not cfg.crewai_enabled:
        yield _check(
            id="completion.adapter",
            category="Completion",
            status=DoctorStatus.FAIL,
            summary="CrewAI terminal present without explicit selection",
            details=details,
            remediation="Set integrations.crewai.enabled: true",
            evidence=EVIDENCE_RUNTIME,
        )
        return

    selected_frameworks = sorted(adapters & framework_adapters)
    summary = (
        f"Framework terminal adapter(s) verified: {selected_frameworks}"
        if selected_frameworks
        else f"Terminal adapter(s) verified: {sorted(adapters)}"
    )
    if custom and not selected_frameworks:
        details += (
            "; custom adapters are verified only via registration metadata "
            "(wrap_final_message/gate_graph_end still required in your runtime)"
        )
    yield _check(
        id="completion.adapter",
        category="Completion",
        status=DoctorStatus.PASS,
        summary=summary,
        details=details,
        evidence=evidence,
    )


@doctor_check("budget")
def check_budget(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    import importlib

    from mycelium.config import _budget_ceilings_from_config

    cfg = ctx.config
    if cfg.budget is None:
        yield _check(
            id="budget.skipped",
            category="Budget",
            status=DoctorStatus.SKIP,
            summary="budget: not configured",
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        return

    ceilings = _budget_ceilings_from_config(cfg.budget)
    token_or_cost = ceilings.requires_usage_meter()
    missing_usage = _missing_usage_policy(cfg.budget, profile=cfg.profile)
    adapters = set(cfg._llm_adapters)
    budget_llm_mod = importlib.import_module("mycelium.budget_llm")
    measures_tokens = "langgraph" in adapters
    measures_cost = bool(budget_llm_mod._cost_resolvers)
    for name in adapters:
        adapter = budget_llm_mod._registered_llm_adapters.get(name)
        if adapter is None:
            continue
        measures_tokens = measures_tokens or adapter.measures_tokens
        if adapter.resolve_cost is not None:
            measures_cost = True

    details = (
        f"adapters={sorted(adapters) or 'none'}; "
        f"max_steps={ceilings.max_steps}; max_tokens={ceilings.max_tokens}; "
        f"max_usd={ceilings.max_usd}; max_duration={ceilings.max_duration}; "
        "step_unit=one protected tool call or instrumented LLM turn; "
        "business workflow counters are separate; "
        f"missing_usage_policy={missing_usage}; "
        f"measures_tokens={measures_tokens}; measures_cost={measures_cost}"
    )

    if token_or_cost and not adapters:
        status = (
            DoctorStatus.FAIL
            if cfg.profile == PROFILE_PRODUCTION
            else DoctorStatus.WARN
        )
        yield _check(
            id="budget.adapter",
            category="Budget",
            status=status,
            summary="Budget token/cost limits without LLM adapter",
            details=details,
            remediation=(
                "Set integrations.langgraph.enabled: true or "
                "register_llm_budget_adapter(...) before load_config(). "
                "Having LangGraph installed is not enough. Doctor does not "
                "call an LLM."
            ),
            evidence=EVIDENCE_RUNTIME,
        )
        return

    if ceilings.max_usd is not None and not measures_cost:
        status = (
            DoctorStatus.FAIL
            if cfg.profile == PROFILE_PRODUCTION
            else DoctorStatus.WARN
        )
        yield _check(
            id="budget.cost_resolver",
            category="Budget",
            status=status,
            summary="Cost limit set without a cost resolver",
            details=details,
            remediation=(
                "Call register_llm_cost_resolver(...) or "
                "register_llm_budget_adapter(..., resolve_cost=...) before "
                "load_config(). Mycelium never invents prices."
            ),
            evidence=EVIDENCE_RUNTIME,
        )
        return

    if ceilings.max_tokens is not None and adapters and not measures_tokens:
        yield _check(
            id="budget.token_meter",
            category="Budget",
            status=DoctorStatus.FAIL,
            summary="Token limit set but adapter cannot measure tokens",
            details=details,
            remediation="Register an adapter with measures_tokens=True.",
            evidence=EVIDENCE_RUNTIME,
        )
        return

    if (
        cfg.profile == PROFILE_PRODUCTION
        and token_or_cost
        and missing_usage != "error"
    ):
        yield _check(
            id="budget.missing_usage",
            category="Budget",
            status=DoctorStatus.FAIL,
            summary="Production token/cost budget allows unknown accounting",
            details=details,
            remediation="Set budget.missing_usage_policy: error",
            evidence=EVIDENCE_STATIC,
        )
        return

    if not token_or_cost:
        yield _check(
            id="budget.adapter",
            category="Budget",
            status=DoctorStatus.PASS,
            summary="Protected-call/time budget configured (no token meter required)",
            details=details,
            evidence=EVIDENCE_STATIC,
        )
        return

    yield _check(
        id="budget.adapter",
        category="Budget",
        status=DoctorStatus.PASS,
        summary="LLM boundary and usage adapter verified",
        details=details,
        evidence=EVIDENCE_RUNTIME,
    )


@doctor_check("outcomes")
def check_outcomes(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    cfg = ctx.config
    raw = cfg.outcome_emit
    if raw is None:
        if cfg.profile == PROFILE_PRODUCTION:
            yield _check(
                id="outcomes.missing",
                category="Outcomes",
                status=DoctorStatus.FAIL,
                summary="outcome_emit: missing in production",
                remediation=(
                    "Add outcome_emit with storage: postgres (recommended), "
                    "redis + persistence: required, or file (single-node)."
                ),
                evidence=EVIDENCE_STATIC,
            )
        else:
            yield _check(
                id="outcomes.missing",
                category="Outcomes",
                status=DoctorStatus.WARN,
                summary="outcome_emit: not configured",
                remediation="Enable outcome_emit for durable decision evidence.",
                evidence=EVIDENCE_STATIC,
                blocking=False,
            )
        return

    storage = _storage_type(raw)
    on_failure = _outcome_on_failure(raw, profile=cfg.profile)
    label = safe_backend_label(raw)

    if storage == "memory":
        status = (
            DoctorStatus.FAIL
            if cfg.profile == PROFILE_PRODUCTION
            else DoctorStatus.WARN
        )
        yield _check(
            id="outcomes.backend",
            category="Outcomes",
            status=status,
            summary="Outcome storage is memory (not durable)",
            details=f"storage=memory on_failure={on_failure}",
            remediation="Use postgres, redis (with persistence: required), or file.",
            evidence=EVIDENCE_STATIC,
        )
        return

    if storage == "file":
        yield _check(
            id="outcomes.backend",
            category="Outcomes",
            status=DoctorStatus.PASS,
            summary="File durable outcome backend configured (single-node)",
            details=f"{label}; on_failure={on_failure}",
            remediation=(
                "File is not cross-node storage. Prefer postgres for "
                "distributed deployments."
            ),
            evidence=EVIDENCE_STATIC,
        )
    elif storage == "postgres":
        try:
            resolve_storage_url(raw, url_key="url", alt_keys=("dsn",))
        except ValueError as exc:
            yield _check(
                id="outcomes.backend",
                category="Outcomes",
                status=DoctorStatus.FAIL,
                summary="Postgres outcome config incomplete",
                details=str(exc),
                remediation="Set url/url_env or dsn/dsn_env for outcome_emit.",
                evidence=EVIDENCE_STATIC,
            )
            return
        yield _check(
            id="outcomes.backend",
            category="Outcomes",
            status=DoctorStatus.PASS,
            summary="PostgreSQL durable outcome backend configured",
            details=f"{label}; on_failure={on_failure}",
            evidence=EVIDENCE_STATIC,
        )
    elif storage == "redis":
        try:
            resolve_storage_url(raw, url_key="url")
        except ValueError as exc:
            yield _check(
                id="outcomes.backend",
                category="Outcomes",
                status=DoctorStatus.FAIL,
                summary="Redis outcome config incomplete",
                details=str(exc),
                remediation="Set url or url_env for outcome_emit.",
                evidence=EVIDENCE_STATIC,
            )
            return
        persistence = raw.get("persistence")
        if persistence != "required":
            status = (
                DoctorStatus.FAIL
                if cfg.profile == PROFILE_PRODUCTION
                else DoctorStatus.WARN
            )
            yield _check(
                id="outcomes.backend",
                category="Outcomes",
                status=status,
                summary="Redis outcomes lack persistence: required",
                details=f"{label}; persistence={persistence!r}",
                remediation=(
                    "Set outcome_emit.persistence: required and enable AOF "
                    "(or equivalently durable Redis). Mycelium cannot verify "
                    "the server's durability policy."
                ),
                evidence=EVIDENCE_OPERATOR,
            )
            return
        yield _check(
            id="outcomes.backend",
            category="Outcomes",
            status=DoctorStatus.PASS,
            summary="Redis Streams outcome backend configured",
            details=(
                f"{label}; persistence=required; on_failure={on_failure}. "
                "Durability is an operator assertion — Mycelium cannot verify AOF."
            ),
            evidence=EVIDENCE_OPERATOR,
        )
    else:
        yield _check(
            id="outcomes.backend",
            category="Outcomes",
            status=DoctorStatus.FAIL,
            summary=f"Unknown outcome storage {storage!r}",
            remediation="Use memory|file|postgres|redis",
            evidence=EVIDENCE_STATIC,
        )
        return

    if cfg.profile == PROFILE_PRODUCTION and on_failure != OUTCOME_ON_FAILURE_ERROR:
        yield _check(
            id="outcomes.on_failure",
            category="Outcomes",
            status=DoctorStatus.FAIL,
            summary="Production outcome on_failure is not error",
            details=f"on_failure={on_failure!r}",
            remediation="Set outcome_emit.on_failure: error",
            evidence=EVIDENCE_STATIC,
        )

    # Contract surface (optional drivers may be absent in unit environments)
    try:
        storage_obj = MyceliumConfig._build_outcome_storage(raw)
    except ImportError as exc:
        yield _check(
            id="outcomes.contract",
            category="Outcomes",
            status=DoctorStatus.WARN,
            summary="Outcome storage driver not installed in this environment",
            details=redact_secrets(str(exc)),
            remediation=(
                "Install mycelium-runtime[postgres] or [redis] where that "
                "backend is used. Static config checks above still apply."
            ),
            evidence=EVIDENCE_NOT_VERIFIABLE,
            blocking=False,
        )
        probe = _probe_storage(ctx, raw=raw, label="outcome_emit")
        if probe is not None:
            yield probe
        return
    except Exception as exc:
        yield _check(
            id="outcomes.contract",
            category="Outcomes",
            status=DoctorStatus.FAIL,
            summary="Outcome storage could not be constructed",
            details=redact_secrets(str(exc)),
            remediation="Fix outcome_emit storage settings.",
            evidence=EVIDENCE_STATIC,
        )
        return

    if not isinstance(storage_obj, OutcomeStorage):
        yield _check(
            id="outcomes.contract",
            category="Outcomes",
            status=DoctorStatus.FAIL,
            summary="Outcome backend is not an OutcomeStorage",
            evidence=EVIDENCE_STATIC,
        )
        return

    has_append = callable(getattr(storage_obj, "append", None))
    has_list = callable(getattr(storage_obj, "list_all", None))
    kind = type(storage_obj).__name__
    if not (has_append and has_list):
        yield _check(
            id="outcomes.contract",
            category="Outcomes",
            status=DoctorStatus.FAIL,
            summary="Outcome backend missing append/list_all",
            details=kind,
            evidence=EVIDENCE_STATIC,
        )
    else:
        if isinstance(storage_obj, InMemoryOutcomeStorage):
            concrete = "InMemoryOutcomeStorage"
        elif isinstance(storage_obj, FileOutcomeStorage):
            concrete = "FileOutcomeStorage"
        elif isinstance(storage_obj, PostgresOutcomeStorage):
            concrete = "PostgresOutcomeStorage"
        elif isinstance(storage_obj, RedisOutcomeStorage):
            concrete = "RedisOutcomeStorage"
        else:
            concrete = kind
        yield _check(
            id="outcomes.contract",
            category="Outcomes",
            status=DoctorStatus.PASS,
            summary=f"OutcomeStorage contract exposed ({concrete})",
            details="append/list_all present",
            evidence=EVIDENCE_STATIC,
        )

    probe = _probe_storage(ctx, raw=raw, label="outcome_emit")
    if probe is not None:
        yield probe


@doctor_check("state_backend")
def check_state_backend(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    cfg = ctx.config
    raw = cfg.state_backend
    if raw is None:
        yield _check(
            id="state_backend.skipped",
            category="Shared state",
            status=DoctorStatus.SKIP,
            summary="state_backend: not configured",
            remediation=(
                "Configure state_backend for shared loop, scope, completion, "
                "state-flush, and audit-receipt state."
            ),
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        return

    storage = _storage_type(raw)
    namespace = str(raw.get("namespace", "mycelium"))
    topology = (cfg.deployment or {}).get("topology")
    if storage == "memory":
        status = DoctorStatus.FAIL if topology == "multi_node" else DoctorStatus.WARN
        yield _check(
            id="state_backend.backend",
            category="Shared state",
            status=status,
            summary="Shared state backend is process-local memory",
            details=f"namespace={namespace!r}",
            remediation="Use file for single-node durability or Redis/Postgres for multi-node.",
            evidence=EVIDENCE_STATIC,
            blocking=status == DoctorStatus.FAIL,
        )
        return
    if storage in ("file",):
        status = DoctorStatus.FAIL if topology == "multi_node" else DoctorStatus.PASS
        yield _check(
            id="state_backend.backend",
            category="Shared state",
            status=status,
            summary="File shared-state backend configured (single-node only)",
            details=f"{safe_backend_label(raw)}; namespace={namespace!r}",
            remediation="Use Redis or Postgres before running multiple workers.",
            evidence=EVIDENCE_STATIC,
        )
    elif storage in DISTRIBUTED_STORAGES:
        try:
            if storage == "postgres":
                resolve_storage_url(raw, url_key="dsn", alt_keys=("url",))
            else:
                resolve_storage_url(raw)
        except ValueError as exc:
            yield _check(
                id="state_backend.backend",
                category="Shared state",
                status=DoctorStatus.FAIL,
                summary=f"{storage} shared-state configuration is incomplete",
                details=redact_secrets(str(exc)),
                remediation="Configure url/url_env or dsn/dsn_env.",
                evidence=EVIDENCE_STATIC,
            )
            return
        yield _check(
            id="state_backend.backend",
            category="Shared state",
            status=DoctorStatus.PASS,
            summary=f"{storage} shared-state backend configured",
            details=f"{safe_backend_label(raw)}; namespace={namespace!r}; CAS=required",
            evidence=EVIDENCE_STATIC,
        )
    else:
        yield _check(
            id="state_backend.backend",
            category="Shared state",
            status=DoctorStatus.FAIL,
            summary=f"Unknown shared-state backend {storage!r}",
            evidence=EVIDENCE_STATIC,
        )
        return

    probe = _probe_storage(ctx, raw=raw, label="state_backend")
    if probe is not None:
        yield probe


@doctor_check("topology")
def check_topology(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    cfg = ctx.config
    deployment = cfg.deployment or {}
    topology = deployment.get("topology") if isinstance(deployment, dict) else None

    shared_sections: list[tuple[str, str]] = []
    consequential = _consequential_tools(cfg)
    if consequential:
        storage = _storage_type(_ledger_storage_for_tool(cfg, consequential[0]))
        shared_sections.append(("action_ledger", storage))
    if cfg.outcome_emit is not None:
        shared_sections.append(("outcome_emit", _storage_type(cfg.outcome_emit)))
    if cfg.loop_guard is not None:
        shared_sections.append(("loop_guard", _guard_storage_type(cfg, cfg.loop_guard)))
    if cfg.scope_guard is not None:
        shared_sections.append(("scope_guard", _guard_storage_type(cfg, cfg.scope_guard)))
    if cfg.budget is not None:
        shared_sections.append(("budget", _storage_type(cfg.budget)))
    if cfg.completion is not None:
        shared_sections.append(("completion", _guard_storage_type(cfg, cfg.completion)))
    if cfg.state_flush is not None:
        shared_sections.append(("state_flush", _guard_storage_type(cfg, cfg.state_flush)))
    if cfg.audit_receipt is not None:
        shared_sections.append(("audit_receipt", _guard_storage_type(cfg, cfg.audit_receipt)))

    single_node_used = [
        f"{name}={storage}"
        for name, storage in shared_sections
        if storage in ("file", "sqlite")
    ]
    memory_used = [
        f"{name}=memory"
        for name, storage in shared_sections
        if storage == "memory"
    ]

    if topology is None:
        yield _check(
            id="topology.omitted",
            category="Deployment topology",
            status=DoctorStatus.WARN,
            summary="deployment.topology omitted; distributed suitability unknown",
            details=(
                f"shared backends: {shared_sections or 'none'}; "
                f"single-node: {single_node_used or 'none'}"
            ),
            remediation=(
                "Set deployment.topology: single_node or multi_node so doctor "
                "can enforce shared-backend requirements."
            ),
            evidence=EVIDENCE_NOT_VERIFIABLE,
            blocking=False,
        )
        return

    if topology not in ("single_node", "multi_node"):
        yield _check(
            id="topology.invalid",
            category="Deployment topology",
            status=DoctorStatus.FAIL,
            summary=f"Invalid deployment.topology {topology!r}",
            remediation="Use single_node or multi_node.",
            evidence=EVIDENCE_STATIC,
        )
        return

    if topology == "single_node":
        yield _check(
            id="topology.single_node",
            category="Deployment topology",
            status=DoctorStatus.PASS,
            summary="deployment.topology=single_node",
            details=f"backends={shared_sections}",
            evidence=EVIDENCE_OPERATOR,
        )
        return

    # multi_node
    problems = list(single_node_used) + list(memory_used)
    if problems:
        yield _check(
            id="topology.multi_node",
            category="Deployment topology",
            status=DoctorStatus.FAIL,
            summary="multi_node topology uses non-shared storage",
            details=", ".join(problems),
            remediation=(
                "For multi_node, use postgres/redis for ActionLedger and "
                "outcome_emit (and other shared guards). File/SQLite are "
                "single-node only."
            ),
            evidence=EVIDENCE_STATIC,
        )
        return

    yield _check(
        id="topology.multi_node",
        category="Deployment topology",
        status=DoctorStatus.PASS,
        summary="multi_node topology uses shared backends",
        details=f"backends={shared_sections}",
        evidence=EVIDENCE_OPERATOR,
    )


@doctor_check("secret_args")
def check_secret_args(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    """AF-010: report secret-in-args scanning, fail-closed production, allowlists."""
    from mycelium.budget_llm import registered_llm_budget_adapters
    from mycelium.completion_contract import registered_terminal_adapters
    from mycelium.secret_protection import registered_secret_resolver

    cfg = ctx.config
    raw = cfg.secret_args
    consequential = _consequential_tools(cfg)
    declared_fields = [
        f"{name}:{','.join(tool.secret_fields)}"
        for name, tool in cfg.tools.items()
        if tool.secret_fields
    ]

    if raw is None:
        yield _check(
            id="secrets.scanning",
            category="Secret-in-args",
            status=DoctorStatus.SKIP,
            summary="secret_args is not configured (existing behavior)",
            details=(
                "Omitted secret_args preserves pre-AF-010 behavior. "
                f"consequential_tools={consequential or 'none'}."
            ),
            remediation=(
                "Add secret_args: {enabled: true, policy: error} so raw "
                "credentials are blocked before claim. Pass secret:// "
                "references instead of credentials."
            ),
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        if cfg.profile == PROFILE_PRODUCTION and consequential:
            yield _check(
                id="secrets.production_fail_closed",
                category="Secret-in-args",
                status=DoctorStatus.SKIP,
                summary="Production consequential tools do not fail closed on secrets",
                details=(
                    "profile=production but secret_args is omitted; "
                    f"tools={consequential}. This is not a promised production default."
                ),
                remediation="Enable secret_args.policy: error for consequential tools.",
                evidence=EVIDENCE_STATIC,
                blocking=False,
            )
    else:
        enabled = bool(raw.get("enabled", True))
        policy = str(raw.get("policy", "error"))
        allow_fields = list(raw.get("allow_fields") or [])
        allow_tools = list(raw.get("allow_tools") or [])
        yield _check(
            id="secrets.scanning",
            category="Secret-in-args",
            status=DoctorStatus.PASS if enabled else DoctorStatus.WARN,
            summary=(
                "Secret scanning is enabled"
                if enabled
                else "Secret scanning is configured but disabled"
            ),
            details=(
                f"enabled={enabled}; policy={policy!r}; "
                f"allow_fields={allow_fields or 'none'}; "
                f"allow_tools={allow_tools or 'none'}; "
                f"entropy_detection={raw.get('entropy_detection', True)}"
            ),
            remediation="" if enabled else "Set secret_args.enabled: true.",
            evidence=EVIDENCE_STATIC,
            blocking=not enabled,
        )
        fail_closed = enabled and policy == "error"
        if cfg.profile == PROFILE_PRODUCTION and consequential:
            if fail_closed:
                yield _check(
                    id="secrets.production_fail_closed",
                    category="Secret-in-args",
                    status=DoctorStatus.PASS,
                    summary="Production consequential tools fail closed on raw secrets",
                    details=f"policy=error; tools={consequential}",
                    evidence=EVIDENCE_STATIC,
                )
            else:
                yield _check(
                    id="secrets.production_fail_closed",
                    category="Secret-in-args",
                    status=DoctorStatus.FAIL,
                    summary="Production consequential tools do not fail closed",
                    details=f"policy={policy!r}; tools={consequential}",
                    remediation="Set secret_args.policy: error under profile: production.",
                    evidence=EVIDENCE_STATIC,
                )
        if allow_fields:
            yield _check(
                id="secrets.allow_fields",
                category="Secret-in-args",
                status=DoctorStatus.WARN,
                summary="Global allow_fields weakens secret-in-args protection",
                details=(
                    f"allow_fields={allow_fields}. Scope allowlists narrowly "
                    "by tool (tools.<name>.secret_fields), not as a global trust list."
                ),
                remediation="Prefer per-tool secret_fields and empty global allow_fields.",
                evidence=EVIDENCE_STATIC,
                blocking=False,
            )
        if allow_tools:
            overlap = [name for name in allow_tools if name in consequential]
            yield _check(
                id="secrets.allow_tools",
                category="Secret-in-args",
                status=DoctorStatus.WARN,
                summary="allow_tools skips secret scanning",
                details=(
                    f"allow_tools={allow_tools}; consequential_exempt={overlap or 'none'}"
                ),
                remediation="Do not exempt consequential tools from secret scanning.",
                evidence=EVIDENCE_STATIC,
                blocking=False,
            )

    resolver = registered_secret_resolver()
    if declared_fields:
        if resolver is None:
            yield _check(
                id="secrets.resolver",
                category="Secret-in-args",
                status=DoctorStatus.WARN,
                summary="Tools declare secret_fields but no resolver is registered",
                details=f"declared={declared_fields}",
                remediation="Call register_secret_resolver before resolving secret:// refs.",
                evidence=EVIDENCE_RUNTIME,
                blocking=False,
            )
        else:
            yield _check(
                id="secrets.resolver",
                category="Secret-in-args",
                status=DoctorStatus.PASS,
                summary="Secret resolver is registered for declared secret_fields",
                details=f"declared={declared_fields}",
                evidence=EVIDENCE_RUNTIME,
            )
    elif resolver is not None:
        yield _check(
            id="secrets.resolver",
            category="Secret-in-args",
            status=DoctorStatus.PASS,
            summary="Secret resolver is registered",
            evidence=EVIDENCE_RUNTIME,
        )
    else:
        yield _check(
            id="secrets.resolver",
            category="Secret-in-args",
            status=DoctorStatus.SKIP,
            summary="No secret resolver registered (none required)",
            details="Applications must register a resolver explicitly; none is built in.",
            evidence=EVIDENCE_RUNTIME,
            blocking=False,
        )

    custom_adapters = sorted(
        set(registered_terminal_adapters()) | set(registered_llm_budget_adapters())
    )
    if custom_adapters:
        yield _check(
            id="secrets.custom_adapters",
            category="Secret-in-args",
            status=DoctorStatus.WARN,
            summary="Custom adapters cannot be verified to sanitize evidence",
            details=f"adapters={custom_adapters}",
            remediation=(
                "Host-owned adapters must call sanitize_secrets before emitting "
                "or logging tool arguments."
            ),
            evidence=EVIDENCE_NOT_VERIFIABLE,
            blocking=False,
        )
    else:
        yield _check(
            id="secrets.custom_adapters",
            category="Secret-in-args",
            status=DoctorStatus.PASS,
            summary="No custom evidence adapters registered",
            evidence=EVIDENCE_RUNTIME,
        )

    yield _check(
        id="secrets.host_logs",
        category="Secret-in-args",
        status=DoctorStatus.SKIP,
        summary="Host logs and third-party providers are not verifiable",
        details=(
            "Mycelium cannot inspect application logs, provider SDKs, or "
            "operator terminals. Redaction is defense-in-depth; fail-closed "
            "pre-execution blocking is the primary protection."
        ),
        evidence=EVIDENCE_NOT_VERIFIABLE,
        blocking=False,
    )


@doctor_check("entity_guard")
def check_entity_guard(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    """Destination policy: writes may only reach host-authorized entities."""
    cfg = ctx.config
    raw = cfg.entity_guard
    consequential = _consequential_tools(cfg)
    declared = sorted((raw or {}).get("tools") or {}) if raw else []
    missing = [name for name in consequential if name not in declared]

    if raw is None:
        yield _check(
            id="entity.scanning",
            category="Entity-guard",
            status=DoctorStatus.SKIP,
            summary="entity_guard is not configured (existing behavior)",
            details=f"consequential_tools={consequential or 'none'}.",
            remediation=(
                "Add entity_guard: {enabled: true, missing_policy: error} "
                "and declare destination paths per write tool. The model "
                "must never add recipients to the allowlist."
            ),
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        return

    enabled = bool(raw.get("enabled", True))
    missing_policy = str(raw.get("missing_policy", "error"))
    yield _check(
        id="entity.scanning",
        category="Entity-guard",
        status=DoctorStatus.PASS if enabled else DoctorStatus.WARN,
        summary=(
            "Destination policy is enabled"
            if enabled
            else "Destination policy is configured but disabled"
        ),
        details=(
            f"enabled={enabled}; missing_policy={missing_policy!r}; "
            f"tools={declared or 'none'}"
        ),
        remediation="" if enabled else "Set entity_guard.enabled: true.",
        evidence=EVIDENCE_STATIC,
        blocking=not enabled,
    )
    fail_closed = enabled and missing_policy == "error"
    if cfg.profile == PROFILE_PRODUCTION and declared:
        yield _check(
            id="entity.production_fail_closed",
            category="Entity-guard",
            status=DoctorStatus.PASS if fail_closed else DoctorStatus.FAIL,
            summary=(
                "Production destination checks fail closed"
                if fail_closed
                else "Production destination checks do not fail closed"
            ),
            details=f"missing_policy={missing_policy!r}; tools={declared}",
            remediation=""
            if fail_closed
            else "Set entity_guard.missing_policy: error under profile: production.",
            evidence=EVIDENCE_STATIC,
        )
    if enabled and missing:
        yield _check(
            id="entity.undeclared_tools",
            category="Entity-guard",
            status=DoctorStatus.WARN,
            summary="Consequential tools have no destination declaration",
            details=f"undeclared={missing}",
            remediation=(
                "Declare entity_guard.tools.<name>.destinations for each "
                "write tool. Unknown destination means no execution."
            ),
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
    elif enabled and declared:
        yield _check(
            id="entity.undeclared_tools",
            category="Entity-guard",
            status=DoctorStatus.PASS,
            summary="Listed write tools declare destination paths",
            details=f"tools={declared}",
            evidence=EVIDENCE_STATIC,
        )
    yield _check(
        id="entity.host_owned",
        category="Entity-guard",
        status=DoctorStatus.PASS if enabled else DoctorStatus.SKIP,
        summary="Destination allowlists are host-controlled",
        details="The model cannot add recipients, hosts, or entity IDs to the allowlist.",
        evidence=EVIDENCE_STATIC,
        blocking=False,
    )


@doctor_check("destructive_confirm")
def check_destructive_confirm(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    """Object-specific grants: tool permission is not object authorization."""
    cfg = ctx.config
    raw = cfg.destructive_confirm
    if raw is None:
        yield _check(
            id="destructive.scanning",
            category="Destructive-confirm",
            status=DoctorStatus.SKIP,
            summary="destructive_confirm is not configured (existing behavior)",
            details="Omitted destructive_confirm preserves pre-AF-011 behavior.",
            remediation=(
                "Add destructive_confirm: {enabled: true, missing_policy: error} "
                "and declare operation + object id_from per destructive tool. "
                "The model cannot mint grants."
            ),
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        return

    enabled = bool(raw.get("enabled", True))
    missing_policy = str(raw.get("missing_policy", "error"))
    storage = str(raw.get("storage", STORAGE_MEMORY))
    tools_raw = raw.get("tools") or {}
    declared = sorted(tools_raw)
    exact = []
    incomplete = []
    require_canon = []
    binds_request = []
    for name, tool_raw in tools_raw.items():
        obj = (tool_raw or {}).get("object") or {}
        grant = (tool_raw or {}).get("grant") or {}
        if (
            tool_raw.get("operation")
            and obj.get("type")
            and obj.get("id_from")
        ):
            exact.append(name)
        else:
            incomplete.append(name)
        if obj.get("require_canonicalizer"):
            require_canon.append((name, obj.get("type")))
        if grant.get("bind_request_id") or grant.get("bind_run_id"):
            binds_request.append(name)

    yield _check(
        id="destructive.enabled",
        category="Destructive-confirm",
        status=DoctorStatus.PASS if enabled else DoctorStatus.WARN,
        summary=(
            "Destructive confirmation is enabled"
            if enabled
            else "Destructive confirmation is configured but disabled"
        ),
        details=(
            f"enabled={enabled}; missing_policy={missing_policy!r}; "
            f"storage={storage!r}; tools={declared or 'none'}"
        ),
        remediation="" if enabled else "Set destructive_confirm.enabled: true.",
        evidence=EVIDENCE_STATIC,
        blocking=not enabled,
    )
    fail_closed = enabled and missing_policy == "error"
    if cfg.profile == PROFILE_PRODUCTION and declared:
        yield _check(
            id="destructive.production_fail_closed",
            category="Destructive-confirm",
            status=DoctorStatus.PASS if fail_closed else DoctorStatus.FAIL,
            summary=(
                "Production destructive checks fail closed"
                if fail_closed
                else "Production destructive checks do not fail closed"
            ),
            details=f"missing_policy={missing_policy!r}",
            remediation=""
            if fail_closed
            else "Set destructive_confirm.missing_policy: error under profile: production.",
            evidence=EVIDENCE_STATIC,
        )
        durable = storage != STORAGE_MEMORY
        yield _check(
            id="destructive.production_storage",
            category="Destructive-confirm",
            status=DoctorStatus.PASS if durable else DoctorStatus.FAIL,
            summary=(
                "Production grant storage is durable"
                if durable
                else "Production grant storage is memory"
            ),
            details=f"storage={storage!r}",
            remediation=""
            if durable
            else "Use file, sqlite, redis, or postgres grant storage in production.",
            evidence=EVIDENCE_STATIC,
        )
        topology = (cfg.deployment or {}).get("topology")
        if topology == "multi_node":
            shared = storage in SHARED_GRANT_STORAGES
            yield _check(
                id="destructive.shared_storage",
                category="Destructive-confirm",
                status=DoctorStatus.PASS if shared else DoctorStatus.FAIL,
                summary=(
                    "multi_node grant storage is shared"
                    if shared
                    else "multi_node grant storage is single-node"
                ),
                details=f"storage={storage!r}; topology={topology}",
                remediation=""
                if shared
                else "Use redis or postgres grant storage for multi-node production.",
                evidence=EVIDENCE_STATIC,
            )
        elif storage in SINGLE_NODE_GRANT_STORAGES and topology != "single_node":
            yield _check(
                id="destructive.shared_storage",
                category="Destructive-confirm",
                status=DoctorStatus.WARN,
                summary="Grant storage is single-node; topology is not declared multi_node",
                details=f"storage={storage!r}; topology={topology!r}",
                remediation="Set deployment.topology: single_node or use redis/postgres.",
                evidence=EVIDENCE_STATIC,
                blocking=False,
            )

    if enabled and incomplete:
        yield _check(
            id="destructive.object_identity",
            category="Destructive-confirm",
            status=DoctorStatus.FAIL,
            summary="Configured destructive tools do not identify an exact object",
            details=f"incomplete={incomplete}",
            remediation=(
                "Set operation, object.type, and object.id_from on each "
                "destructive_confirm.tools entry."
            ),
            evidence=EVIDENCE_STATIC,
        )
    elif enabled and exact:
        yield _check(
            id="destructive.object_identity",
            category="Destructive-confirm",
            status=DoctorStatus.PASS,
            summary="Configured destructive tools identify an exact object",
            details=f"tools={exact}",
            evidence=EVIDENCE_STATIC,
        )

    irreversible = [
        name
        for name, tool in cfg.tools.items()
        if tool.side_effect_class == SideEffectClass.IRREVERSIBLE
        and name not in declared
        and tool.destructive_confirm is not False
    ]
    if enabled and irreversible:
        yield _check(
            id="destructive.irreversible_declared",
            category="Destructive-confirm",
            status=DoctorStatus.WARN
            if cfg.profile != PROFILE_PRODUCTION
            else DoctorStatus.FAIL,
            summary="Irreversible tools have no destructive_confirm declaration",
            details=f"undeclared={irreversible}",
            remediation=(
                "Declare destructive_confirm.tools.<name> with operation and "
                "object.id_from. Do not infer destructiveness from the tool name."
            ),
            evidence=EVIDENCE_STATIC,
            blocking=cfg.profile == PROFILE_PRODUCTION,
        )

    if enabled and declared and not binds_request:
        yield _check(
            id="destructive.bindings",
            category="Destructive-confirm",
            status=DoctorStatus.WARN,
            summary="No destructive tool binds request_id or run_id",
            details=f"tools={declared}",
            remediation="Set grant.bind_request_id: true so retries reuse one grant use.",
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
    elif enabled and binds_request:
        yield _check(
            id="destructive.bindings",
            category="Destructive-confirm",
            status=DoctorStatus.PASS,
            summary="Destructive tools bind request or run identity",
            details=f"tools={binds_request}",
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )

    if enabled and require_canon:
        wired = registered_destructive_canonicalizers()
        missing_canon = [
            f"{name}:{otype}"
            for name, otype in require_canon
            if str(otype).casefold() not in wired
        ]
        yield _check(
            id="destructive.canonicalizer",
            category="Destructive-confirm",
            status=DoctorStatus.WARN if missing_canon else DoctorStatus.PASS,
            summary=(
                "Required custom canonicalizers are not registered in this process"
                if missing_canon
                else "Required custom canonicalizers are registered"
            ),
            details=f"missing={missing_canon or 'none'}; registered={sorted(wired) or 'none'}",
            remediation=(
                "Call register_destructive_object_canonicalizer before load. "
                "Installed or importable adapters are not wired."
            ),
            evidence=EVIDENCE_RUNTIME if missing_canon else EVIDENCE_STATIC,
            blocking=False,
        )

    yield _check(
        id="destructive.host_issued",
        category="Destructive-confirm",
        status=DoctorStatus.SKIP,
        summary="Doctor cannot prove the host mints grants",
        details=(
            "Configuration cannot show that call sites call "
            "issue_destructive_grant, that a human approved the grant, "
            "that the provider honors idempotency keys, or that an "
            "external side effect occurred. Configure host-side approval before "
            "grant issuance or use DualControlOperatorAuthorizer for operator release."
        ),
        evidence=EVIDENCE_NOT_VERIFIABLE,
        blocking=False,
    )


@doctor_check("authority_window")
def check_authority_window(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    """Use-time expiry for time-bounded authority (batch item 4)."""
    from mycelium.authority_window import (
        USE_TIME_CHECK_REQUIRED,
        registered_authority_use_adapters,
    )

    cfg = ctx.config
    raw = cfg.authority_window
    destructive = cfg.destructive_confirm
    if raw is None and destructive is None:
        yield _check(
            id="authority_window.scanning",
            category="Authority-window",
            status=DoctorStatus.SKIP,
            summary="authority_window is not configured (existing behavior)",
            details=(
                "Omitted authority_window preserves timeless paths. "
                "Time-bounded destructive_confirm still enforces use-time "
                "expiry when configured."
            ),
            remediation=(
                "Add authority_window: {enabled: true, use_time_check: required} "
                "when hosts issue time-bounded authority."
            ),
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        return

    enabled = True if raw is None else bool(raw.get("enabled", True))
    use_time = (
        USE_TIME_CHECK_REQUIRED
        if raw is None
        else str(raw.get("use_time_check", USE_TIME_CHECK_REQUIRED))
    )
    skew = 0.0 if raw is None else float(raw.get("clock_skew_tolerance_seconds", 0))
    implied = raw is None and destructive is not None

    yield _check(
        id="authority_window.enabled",
        category="Authority-window",
        status=DoctorStatus.PASS if enabled else DoctorStatus.WARN,
        summary=(
            "Authority-window use-time checks are enabled"
            if enabled
            else "Authority-window is configured but disabled"
        ),
        details=(
            f"enabled={enabled}; use_time_check={use_time!r}; "
            f"clock_skew_tolerance_seconds={skew}; "
            f"implied_from_destructive={implied}"
        ),
        remediation="" if enabled else "Set authority_window.enabled: true.",
        evidence=EVIDENCE_STATIC,
        blocking=not enabled and destructive is not None,
    )

    use_required = enabled and use_time == USE_TIME_CHECK_REQUIRED
    if destructive is not None:
        yield _check(
            id="authority_window.use_time_destructive",
            category="Authority-window",
            status=DoctorStatus.PASS if use_required else DoctorStatus.FAIL,
            summary=(
                "Time-bounded destructive authority requires use-time expiry"
                if use_required
                else "Time-bounded destructive authority does not require use-time expiry"
            ),
            details=f"use_time_check={use_time!r}; enabled={enabled}",
            remediation=""
            if use_required
            else "Set authority_window.use_time_check: required with enabled: true.",
            evidence=EVIDENCE_STATIC,
            blocking=True,
        )

    if cfg.profile == PROFILE_PRODUCTION and destructive is not None:
        fail_closed = use_required
        yield _check(
            id="authority_window.production_fail_closed",
            category="Authority-window",
            status=DoctorStatus.PASS if fail_closed else DoctorStatus.FAIL,
            summary=(
                "Production authority-window fails closed at use"
                if fail_closed
                else "Production authority-window does not fail closed at use"
            ),
            details=f"enabled={enabled}; use_time_check={use_time!r}",
            remediation=""
            if fail_closed
            else "Require use-time expiry under profile: production.",
            evidence=EVIDENCE_STATIC,
        )

    skew_ok = skew >= 0
    yield _check(
        id="authority_window.skew",
        category="Authority-window",
        status=DoctorStatus.PASS if skew_ok else DoctorStatus.FAIL,
        summary=(
            "Clock skew tolerance is non-negative (narrows validity only)"
            if skew_ok
            else "Clock skew tolerance is invalid"
        ),
        details=(
            f"clock_skew_tolerance_seconds={skew}; "
            "skew never extends expired authority"
        ),
        remediation="" if skew_ok else "Set clock_skew_tolerance_seconds >= 0.",
        evidence=EVIDENCE_STATIC,
        blocking=not skew_ok,
    )

    topology = (cfg.deployment or {}).get("topology")
    grant_storage = None
    if destructive is not None:
        grant_storage = str(destructive.get("storage", STORAGE_MEMORY))
    yield _check(
        id="authority_window.clock_assumptions",
        category="Authority-window",
        status=DoctorStatus.PASS,
        summary="Multi-worker clock assumptions are declared for authority expiry",
        details=(
            f"topology={topology!r}; grant_storage={grant_storage!r}; "
            "durable authority uses persisted UTC expires_at; shared Redis/"
            "PostgreSQL stores should prefer authoritative storage time when "
            "available; process-local clocks are not the multi-worker guarantee"
        ),
        evidence=EVIDENCE_STATIC,
        blocking=False,
    )

    adapters = sorted(registered_authority_use_adapters())
    yield _check(
        id="authority_window.adapters",
        category="Authority-window",
        status=DoctorStatus.PASS if not adapters else DoctorStatus.PASS,
        summary=(
            "Custom authority use adapters are registered in this process"
            if adapters
            else "No custom authority use adapters are registered in this process"
        ),
        details=f"registered={adapters or 'none'}; importable adapters are not wired",
        remediation=(
            "Call register_authority_use_adapter before load when a custom "
            "authority kind must participate in use-time checks."
        ),
        evidence=EVIDENCE_RUNTIME if adapters else EVIDENCE_STATIC,
        blocking=False,
    )

    yield _check(
        id="authority_window.batch_incomplete",
        category="Authority-window",
        status=DoctorStatus.PASS
        if getattr(cfg, "use_time_currency", None) is not None
        else DoctorStatus.WARN,
        summary=(
            "Authority-window and use-time currency batch guarantee is complete"
            if getattr(cfg, "use_time_currency", None) is not None
            else (
                "Authority-window is implemented; combined use-time currency "
                "batch guarantee is incomplete"
            )
        ),
        details=(
            "Items 4 (authority-window) and 5 (use-time currency) are both "
            "configured — the combined production authority-safety batch "
            "guarantee is wired."
            if getattr(cfg, "use_time_currency", None) is not None
            else (
                "Item 4 (authority-window expiry) alone does not complete item 5 "
                "(use-time currency). Doctor must not claim the full production "
                "authority-safety guarantee until use-time currency is present. "
                "Do not release until item 5 is implemented."
            )
        ),
        evidence=EVIDENCE_STATIC,
        blocking=False,
    )

    yield _check(
        id="authority_window.not_verifiable",
        category="Authority-window",
        status=DoctorStatus.SKIP,
        summary="Doctor cannot prove end-to-end authority validity",
        details=(
            "not_verifiable: clock synchronization between machines; "
            "host-issued approval timestamps; provider-side authorization "
            "validity; absence of custom code between final validation and "
            "the side effect. Mycelium cannot guarantee authority remains "
            "valid throughout an external network call."
        ),
        evidence=EVIDENCE_NOT_VERIFIABLE,
        blocking=False,
    )


@doctor_check("use_time_currency")
def check_use_time_currency(ctx: DoctorContext) -> Iterable[DoctorCheck]:
    """Use-time currency for decide-time facts (AF-012 / batch item 5)."""
    from mycelium.use_time_currency import registered_use_time_validators

    cfg = ctx.config
    raw = cfg.use_time_currency
    authority = cfg.authority_window
    destructive = cfg.destructive_confirm
    if raw is None:
        yield _check(
            id="use_time_currency.scanning",
            category="Use-time-currency",
            status=DoctorStatus.SKIP,
            summary="use_time_currency is not configured (existing behavior)",
            details=(
                "Omitted use_time_currency preserves decide-time-only paths. "
                "Authority-window expiry alone does not revalidate fact currency."
            ),
            remediation=(
                "Add use_time_currency: {enabled: true, missing_policy: error, "
                "tools: {...}} for consequential tools that depend on "
                "decide-time facts."
            ),
            evidence=EVIDENCE_STATIC,
            blocking=False,
        )
        return

    enabled = bool(raw.get("enabled", True))
    missing_policy = str(raw.get("missing_policy", "error"))
    tools = raw.get("tools") or {}
    validators_declared: set[str] = set()
    for tool_raw in tools.values():
        for fact in tool_raw.get("facts") or []:
            name = fact.get("validator")
            if isinstance(name, str) and name.strip():
                validators_declared.add(name.strip())

    yield _check(
        id="use_time_currency.enabled",
        category="Use-time-currency",
        status=DoctorStatus.PASS if enabled else DoctorStatus.WARN,
        summary=(
            "Use-time currency checks are enabled"
            if enabled
            else "Use-time currency is configured but disabled"
        ),
        details=(
            f"enabled={enabled}; missing_policy={missing_policy!r}; "
            f"tools={sorted(tools) or 'none'}"
        ),
        remediation="" if enabled else "Set use_time_currency.enabled: true.",
        evidence=EVIDENCE_STATIC,
        blocking=False,
    )

    if cfg.profile == PROFILE_PRODUCTION and enabled and tools:
        fail_closed = missing_policy == "error"
        yield _check(
            id="use_time_currency.production_fail_closed",
            category="Use-time-currency",
            status=DoctorStatus.PASS if fail_closed else DoctorStatus.FAIL,
            summary=(
                "Production use-time currency fails closed on missing facts"
                if fail_closed
                else "Production use-time currency does not fail closed"
            ),
            details=f"missing_policy={missing_policy!r}",
            remediation=""
            if fail_closed
            else "Set use_time_currency.missing_policy: error under profile: production.",
            evidence=EVIDENCE_STATIC,
            blocking=True,
        )

    registered = registered_use_time_validators()
    missing = sorted(validators_declared - set(registered))
    yield _check(
        id="use_time_currency.validators",
        category="Use-time-currency",
        status=DoctorStatus.PASS if not missing else DoctorStatus.WARN,
        summary=(
            "Declared use-time validators are registered in this process"
            if not missing
            else "Some declared use-time validators are not registered"
        ),
        details=(
            f"declared={sorted(validators_declared) or 'none'}; "
            f"registered={sorted(registered) or 'none'}; "
            f"missing={missing or 'none'}"
        ),
        remediation=(
            ""
            if not missing
            else "Call register_use_time_validator before load/run for each "
            "validator name listed in use_time_currency.tools."
        ),
        evidence=EVIDENCE_RUNTIME if registered else EVIDENCE_STATIC,
        blocking=False,
    )

    batch_ready = (
        enabled
        and bool(tools)
        and (authority is not None or destructive is not None)
    )
    yield _check(
        id="use_time_currency.batch_complete",
        category="Use-time-currency",
        status=DoctorStatus.PASS if batch_ready else DoctorStatus.WARN,
        summary=(
            "Authority-window and use-time currency batch guarantee is wired"
            if batch_ready
            else "Use-time currency batch guarantee is incomplete"
        ),
        details=(
            "Item 5 is configured with tools and item 4 "
            "(authority_window / destructive_confirm) is also present."
            if batch_ready
            else (
                "Configure use_time_currency.tools and enable authority_window "
                "(or destructive_confirm) so items 4 and 5 complete the batch."
            )
        ),
        evidence=EVIDENCE_STATIC,
        blocking=False,
    )

    yield _check(
        id="use_time_currency.not_verifiable",
        category="Use-time-currency",
        status=DoctorStatus.SKIP,
        summary="Doctor cannot prove end-to-end fact currency",
        details=(
            "not_verifiable: whether host validators query the authoritative "
            "source; replica consistency or cache behavior; correctness of "
            "host-provided fact values; provider conditional-write guarantees; "
            "custom code after final validation; fact changes during a remote "
            "network call. Local revalidation cannot eliminate the remote-call race."
        ),
        evidence=EVIDENCE_NOT_VERIFIABLE,
        blocking=False,
    )


def ensure_builtin_checks_registered() -> None:
    """Import side effect: decorators already registered this module's checks."""
    return None


__all__ = [
    "DISTRIBUTED_STORAGES",
    "SINGLE_NODE_STORAGES",
    "ensure_builtin_checks_registered",
]
