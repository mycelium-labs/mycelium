"""
Failure-mode tests for the public @protect + Session API.

Scenarios align with documented failure modes (e.g. AF-006 / context corruption)
but run only against this package's protect/Session implementation — not the
legacy step-based runtime. FM1 TTL/freshness, FM2 cross-entity, FM3 tool
isolation, FM4 critical reads, FM5 growth bounds, FM6 concurrency, FM7 errors.
"""

from __future__ import annotations

import asyncio

import pytest

from mycelium import Session, protect

_DB = {
    "c1": {"customer_id": "c1", "email": "alice@example.com", "plan": "pro", "revision": 1},
    "c2": {"customer_id": "c2", "email": "bob@example.com", "plan": "free", "revision": 1},
    "c3": {"customer_id": "c3", "email": "carol@example.com", "plan": "basic", "revision": 1},
}
_INVENTORY = {
    "SKU-A": {"product_id": "SKU-A", "units": 50},
    "SKU-B": {"product_id": "SKU-B", "units": 12},
    "SKU-C": {"product_id": "SKU-C", "units": 99},
}
_ORDERS = {
    "c1": ["ORD-001", "ORD-002"],
    "c2": ["ORD-003"],
    "c3": ["ORD-004", "ORD-005", "ORD-006"],
}


# ---------------------------------------------------------------------------
# FM1 — Stale data (TTL / freshness)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm1_never_expiring_naive_fewer_backend_reads_than_ttl_protect() -> None:
    """Naive dict with no TTL: one backend read across six logical calls. @protect(ttl=short) refetches after expiry."""
    state = {"prefs": "light"}
    protect_fetches = [0]
    naive_store: dict[str, dict] = {}
    naive_fetches = [0]

    @protect(entity_param="uid", ttl=0.05)
    async def get_prefs(uid: str) -> dict:
        protect_fetches[0] += 1
        return {"uid": uid, "prefs": state["prefs"]}

    async def naive_get_prefs(uid: str) -> dict:
        if uid in naive_store:
            return dict(naive_store[uid])
        naive_fetches[0] += 1
        r = {"uid": uid, "prefs": state["prefs"]}
        naive_store[uid] = dict(r)
        return dict(r)

    async with Session():
        for _ in range(3):
            await get_prefs(uid="u1")
        await asyncio.sleep(0.11)
        for _ in range(3):
            await get_prefs(uid="u1")

    for _ in range(3):
        await naive_get_prefs("u1")
    await asyncio.sleep(0.11)
    for _ in range(3):
        await naive_get_prefs("u1")

    assert naive_fetches[0] == 1
    assert protect_fetches[0] > naive_fetches[0]


@pytest.mark.asyncio
async def test_fm1_multi_customer_without_stale_preferences_after_nudge() -> None:
    """Without protection, preferences remain stale after backend mutation."""
    prefs = {"theme": "light"}
    naive_cache: dict[str, dict] = {}
    naive_cache["u1"] = dict(prefs)
    prefs["theme"] = "dark"
    assert naive_cache["u1"]["theme"] != prefs["theme"]


@pytest.mark.asyncio
async def test_fm1_multi_customer_with_fresh_preferences_after_ttl() -> None:
    """@protect forces re-fetch after TTL — preferences are fresh after mutation."""
    prefs = {"theme": "light"}

    @protect(entity_param="uid", ttl=0.05)
    async def get_prefs(uid: str) -> dict:
        return dict(prefs)

    async with Session() as s:
        await get_prefs(uid="u1")
        prefs["theme"] = "dark"
        await asyncio.sleep(0.11)
        r = await get_prefs(uid="u1")

    assert r["theme"] == "dark"
    assert "cache_stale" in [e["event"] for e in s.audit_log()]


@pytest.mark.asyncio
async def test_fm1_mid_session_without_keeps_old_email() -> None:
    """Without protection, email stays stale after mid-session backend change."""
    state = {"email": "alice@old.com"}
    naive_cache = {"c1": dict(state)}
    state["email"] = "alice@new.com"
    assert naive_cache["c1"]["email"] == "alice@old.com"


@pytest.mark.asyncio
async def test_fm1_mid_session_with_refetches_email() -> None:
    """@protect re-fetches updated email after TTL expires mid-session."""
    state = {"email": "alice@old.com"}

    @protect(entity_param="customer_id", ttl=0.05)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(state)

    async with Session():
        r1 = await fetch_customer(customer_id="c1")
        assert r1["email"] == "alice@old.com"
        state["email"] = "alice@new.com"
        await asyncio.sleep(0.11)
        r2 = await fetch_customer(customer_id="c1")

    assert r2["email"] == "alice@new.com"


@pytest.mark.asyncio
async def test_fm1_ttl_before_boundary_allows_cache_hit() -> None:
    """Within TTL, @protect returns cached value — emits cache_hit."""

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB[customer_id])

    async with Session() as s:
        await fetch_customer(customer_id="c1")
        await fetch_customer(customer_id="c1")

    assert any(e["event"] == "cache_hit" for e in s.audit_log())


@pytest.mark.asyncio
async def test_fm1_ttl_after_boundary_triggers_refetch_on_change() -> None:
    """After TTL expires, @protect re-fetches — returns mutated value."""
    state = {"email": "old@example.com", "revision": 1}

    @protect(entity_param="customer_id", ttl=0.05)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(state)

    async with Session():
        await fetch_customer(customer_id="c1")
        state["email"] = "new@example.com"
        state["revision"] = 2
        await asyncio.sleep(0.11)
        r = await fetch_customer(customer_id="c1")

    assert r["email"] == "new@example.com"
    assert r["revision"] == 2


# ---------------------------------------------------------------------------
# FM2 — Cross-entity leakage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("c1", "c2"),
        ("c1", "c3"),
        ("c2", "c1"),
        ("c2", "c3"),
        ("c3", "c1"),
        ("c3", "c2"),
        ("c1", "c2"),
    ],
    ids=["e12a", "e13", "e21", "e23", "e31", "e32", "e12b"],
)
@pytest.mark.asyncio
async def test_fm2_entity_snapshots_never_cross_contaminate(left: str, right: str) -> None:
    """@protect(entity_param) gives each customer_id its own cache entry — no leakage."""

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB[customer_id])

    async with Session():
        a = await fetch_customer(customer_id=left)
        b = await fetch_customer(customer_id=right)

    assert a["email"] == _DB[left]["email"]
    assert b["email"] == _DB[right]["email"]
    assert a["customer_id"] != b["customer_id"] or left == right


@pytest.mark.asyncio
async def test_fm2_cache_keys_in_snapshot_are_per_entity() -> None:
    """Three different entity IDs → three live cache entries."""

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB[customer_id])

    async with Session() as s:
        for cid in ("c1", "c2", "c3"):
            await fetch_customer(customer_id=cid)

    assert s.cache_size() >= 3


@pytest.mark.asyncio
async def test_fm2_mutate_one_customer_preserves_other_truth() -> None:
    """Mutating c1's backing data does not change c2's separate cache entry (different entity keys)."""
    store = {
        "c1": {"customer_id": "c1", "email": "alice@example.com"},
        "c2": {"customer_id": "c2", "email": "bob@example.com"},
    }

    @protect(entity_param="customer_id", ttl=0.05)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(store[customer_id])

    async with Session():
        await fetch_customer(customer_id="c1")
        c2_first = await fetch_customer(customer_id="c2")

        store["c1"]["email"] = "alice@updated.com"
        await asyncio.sleep(0.11)

        c2_second = await fetch_customer(customer_id="c2")

    assert c2_second["email"] == store["c2"]["email"]
    assert c2_first["email"] == c2_second["email"]


# ---------------------------------------------------------------------------
# FM3 — Cross-source / tool isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm3_fetch_and_history_same_customer_distinct_cache() -> None:
    """Two tools for the same customer_id use separate cache entries."""

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB[customer_id])

    @protect(entity_param="customer_id", ttl=60)
    async def get_order_history(customer_id: str) -> list[str]:
        return list(_ORDERS[customer_id])

    async with Session() as s:
        p = await fetch_customer(customer_id="c1")
        h = await get_order_history(customer_id="c1")

    assert isinstance(p, dict) and "email" in p
    assert isinstance(h, list)
    assert s.cache_size() >= 2


@pytest.mark.asyncio
async def test_fm3_inventory_vs_customer_no_shared_value() -> None:
    """Inventory and customer tools have different schemas — never mixed."""

    @protect(entity_param="product_id", ttl=60)
    async def check_inventory(product_id: str) -> dict:
        return dict(_INVENTORY[product_id])

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB[customer_id])

    async with Session():
        inv = await check_inventory(product_id="SKU-A")
        cust = await fetch_customer(customer_id="c1")

    assert "units" in inv
    assert "email" in cust
    assert "email" not in inv
    assert "units" not in cust


@pytest.mark.asyncio
async def test_fm3_recommendations_use_customer_entity() -> None:
    """Two customers → two distinct recommendation lists."""

    @protect(entity_param="customer_id", ttl=60)
    async def get_recommendations(customer_id: str) -> list[str]:
        return [f"rec-{customer_id}-{i}" for i in range(3)]

    async with Session():
        r1 = await get_recommendations(customer_id="c1")
        r2 = await get_recommendations(customer_id="c2")

    assert isinstance(r1, list) and isinstance(r2, list)
    assert r1 != r2


@pytest.mark.asyncio
async def test_fm3_order_history_segmented_per_customer() -> None:
    """Order history cached separately per customer — c1 vs c3."""

    @protect(entity_param="customer_id", ttl=60)
    async def get_order_history(customer_id: str) -> list[str]:
        return list(_ORDERS[customer_id])

    async with Session():
        o1 = await get_order_history(customer_id="c1")
        o3 = await get_order_history(customer_id="c3")

    assert o1 != o3


@pytest.mark.asyncio
async def test_fm3_tool_names_distinct_in_audit() -> None:
    """Audit log records distinct tool names for different tools."""

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB[customer_id])

    @protect(entity_param="product_id", ttl=60)
    async def check_inventory(product_id: str) -> dict:
        return dict(_INVENTORY[product_id])

    async with Session() as s:
        await fetch_customer(customer_id="c1")
        await check_inventory(product_id="SKU-B")

    tool_names = {e["tool"] for e in s.audit_log()}
    assert "fetch_customer" in tool_names
    assert "check_inventory" in tool_names


# ---------------------------------------------------------------------------
# FM4 — Critical repeated read / behavioral drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm4_critical_true_sees_mutated_state_immediately() -> None:
    """critical=True always re-fetches — sees updated revision immediately."""
    state = {"email": "alice@old.com", "revision": 1}

    @protect(critical=True)
    async def fetch_customer_critical() -> dict:
        return dict(state)

    async with Session():
        r1 = await fetch_customer_critical()
        state["email"] = "alice@new.com"
        state["revision"] = 2
        r2 = await fetch_customer_critical()

    assert r1["revision"] == 1
    assert r2["revision"] == 2


@pytest.mark.asyncio
async def test_fm4_critical_true_invokes_underlying_fn_each_time() -> None:
    """critical=True bypasses Session cache — each await runs the wrapped function."""
    call_count = [0]

    @protect(critical=True)
    async def fetch_customer() -> dict:
        call_count[0] += 1
        return {"call": call_count[0]}

    async with Session():
        for _ in range(3):
            await fetch_customer()

    assert call_count[0] == 3


@pytest.mark.asyncio
async def test_fm4_naive_dict_revision_stale_after_mutate() -> None:
    """Naive cache: revision stays frozen even after backend mutation."""
    state = {"revision": 1}
    naive_cache: dict[str, dict] = {}
    naive_cache["c2"] = dict(state)
    for _ in range(2):
        _ = naive_cache["c2"]
    state["revision"] = 2
    _ = naive_cache["c2"]
    assert naive_cache["c2"]["revision"] == 1


@pytest.mark.asyncio
async def test_fm4_ttl_protect_sees_updated_revision_after_expiry() -> None:
    """@protect(ttl=short) sees updated revision after TTL expires."""
    state = {"revision": 1}

    @protect(entity_param="customer_id", ttl=0.05)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(state)

    async with Session():
        for _ in range(3):
            await fetch_customer(customer_id="c2")
        state["revision"] = 2
        await asyncio.sleep(0.11)
        last = await fetch_customer(customer_id="c2")

    assert last["revision"] == 2


@pytest.mark.asyncio
async def test_fm4_noncritical_inventory_second_call_is_cache_hit() -> None:
    """Non-critical @protect: two back-to-back calls → second is cache_hit."""
    call_count = [0]

    @protect(entity_param="product_id", ttl=60)
    async def check_inventory(product_id: str) -> dict:
        call_count[0] += 1
        return dict(_INVENTORY[product_id])

    async with Session() as s:
        a = await check_inventory(product_id="SKU-A")
        b = await check_inventory(product_id="SKU-A")

    assert a["product_id"] == b["product_id"]
    assert call_count[0] == 1
    assert any(e["event"] == "cache_hit" for e in s.audit_log())


@pytest.mark.asyncio
async def test_fm4_order_history_long_ttl_stays_cached_across_repeated_calls() -> None:
    """Long TTL (300s): same @protect tool keeps serving from cache across 10 calls (not critical=True)."""
    call_count = [0]

    @protect(entity_param="customer_id", ttl=300)
    async def get_order_history(customer_id: str) -> list[str]:
        call_count[0] += 1
        return list(_ORDERS[customer_id])

    async with Session() as s:
        for _ in range(10):
            await get_order_history(customer_id="c1")

    assert call_count[0] == 1
    assert any(e["event"] == "cache_hit" for e in s.audit_log())


@pytest.mark.asyncio
async def test_fm4_compare_multi_customer_completes() -> None:
    """Three-customer session with @protect completes without error."""

    @protect(entity_param="customer_id", ttl=0.1)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB[customer_id])

    async with Session() as s:
        for cid in ("c1", "c2", "c3"):
            await fetch_customer(customer_id=cid)

    assert s.cache_size() == 3


@pytest.mark.asyncio
async def test_fm4_compare_mid_session_completes() -> None:
    """Mid-session mutation + TTL re-fetch completes without error."""
    state = {"email": "before@example.com"}

    @protect(entity_param="customer_id", ttl=0.05)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(state)

    async with Session() as s:
        await fetch_customer(customer_id="c1")
        state["email"] = "after@example.com"
        await asyncio.sleep(0.11)
        r = await fetch_customer(customer_id="c1")

    assert r["email"] == "after@example.com"
    assert any(e["event"] == "cache_stale" for e in s.audit_log())


# ---------------------------------------------------------------------------
# FM5 — Naive unbounded dict vs TTL + cache_size (live entries)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm5_long_run_without_high_hit_ratio() -> None:
    """Naive cache: every repeat lookup hits the same key (100% of iterations; data can stay stale)."""
    naive_cache: dict[str, dict] = {}
    naive_cache["c1"] = {"email": "alice@example.com"}
    hits = 0
    for _ in range(99):
        if "c1" in naive_cache:
            hits += 1
    assert hits / 99 == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_fm5_ttl_expiry_cache_size_zero_for_three_entities() -> None:
    """After TTL expires, cache_size() is zero (only counts unexpired entries; dict may still hold stale keys)."""

    @protect(entity_param="customer_id", ttl=0.05)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB[customer_id])

    async with Session() as s:
        for cid in ("c1", "c2", "c3"):
            await fetch_customer(customer_id=cid)
        before = s.cache_size()
        await asyncio.sleep(0.11)
        after = s.cache_size()

    assert before == 3
    assert after == 0


@pytest.mark.asyncio
async def test_fm5_without_cache_grows_with_unique_tool_args() -> None:
    """Naive dict cache grows with every unique argument — no eviction."""
    naive_cache: dict[str, dict] = {}
    for pid in ("SKU-A", "SKU-B", "SKU-C"):
        naive_cache[pid] = dict(_INVENTORY[pid])
    assert len(naive_cache) == 3


@pytest.mark.asyncio
async def test_fm5_critical_true_handler_runs_for_each_distinct_call() -> None:
    """critical=True: no caching — two awaits with different args both execute the body."""
    call_count = [0]
    messages: list[str] = []

    @protect(critical=True)
    async def send_email(customer_id: str, message: str) -> dict:
        call_count[0] += 1
        messages.append(message)
        return {"customer_id": customer_id, "message_preview": message[:10]}

    async with Session():
        r1 = await send_email(customer_id="c1", message="Hello there")
        r2 = await send_email(customer_id="c1", message="Different msg")

    assert r1["message_preview"] != r2["message_preview"]
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_fm5_cache_size_is_non_negative() -> None:
    """Session.cache_size() is non-negative empty and after one cached tool."""
    async with Session() as s:
        size_empty = s.cache_size()

        @protect(entity_param="customer_id", ttl=60)
        async def fetch_customer(customer_id: str) -> dict:
            return dict(_DB[customer_id])

        await fetch_customer(customer_id="c1")
        size_with_entry = s.cache_size()

    assert size_empty >= 0
    assert size_with_entry >= 0


# ---------------------------------------------------------------------------
# FM6 — Concurrent access (asyncio interleaving)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm6_gather_distinct_customers_with_protect() -> None:
    """Concurrent fetches for three customers all return correct data."""

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB[customer_id])

    async with Session():
        results = await asyncio.gather(
            fetch_customer(customer_id="c1"),
            fetch_customer(customer_id="c2"),
            fetch_customer(customer_id="c3"),
        )

    assert {r["customer_id"] for r in results} == {"c1", "c2", "c3"}


@pytest.mark.asyncio
async def test_fm6_parallel_inventory_reads_no_errors() -> None:
    """8 concurrent reads of the same inventory item — no errors, all consistent."""

    @protect(entity_param="product_id", ttl=60)
    async def check_inventory(product_id: str) -> dict:
        return dict(_INVENTORY[product_id])

    async with Session():
        rows = await asyncio.gather(*[check_inventory(product_id="SKU-A") for _ in range(8)])

    assert len(rows) == 8
    assert all(r["product_id"] == "SKU-A" for r in rows)


@pytest.mark.asyncio
async def test_fm6_concurrent_same_entity_last_revision_consistent() -> None:
    """5 concurrent reads of same entity return the same revision."""

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB["c1"])

    async with Session():
        revs = await asyncio.gather(*[fetch_customer(customer_id="c1") for _ in range(5)])

    assert all(r["revision"] == revs[0]["revision"] for r in revs)


@pytest.mark.asyncio
async def test_fm6_without_parallel_same_key_all_hits_but_stale_safe_initial() -> None:
    """Naive parallel reads from same cache key: all consistent (but stale)."""
    naive_cache = {"c3": dict(_DB["c3"])}

    async def read() -> dict:
        return naive_cache["c3"]

    rows = await asyncio.gather(*[read() for _ in range(4)])
    assert all(r["email"] == rows[0]["email"] for r in rows)


@pytest.mark.asyncio
async def test_fm6_parallel_recommendations_distinct_customers() -> None:
    """Two customers' recommendations fetched in parallel — both get their own."""

    @protect(entity_param="customer_id", ttl=60)
    async def get_recommendations(customer_id: str) -> list[str]:
        return [f"rec-{customer_id}-{i}" for i in range(3)]

    async with Session():
        a, b = await asyncio.gather(
            get_recommendations(customer_id="c1"),
            get_recommendations(customer_id="c2"),
        )

    assert isinstance(a, list) and isinstance(b, list)
    assert a != b


@pytest.mark.asyncio
async def test_fm6_order_history_parallel_isolated() -> None:
    """Order histories for two customers fetched in parallel — results differ."""

    @protect(entity_param="customer_id", ttl=60)
    async def get_order_history(customer_id: str) -> list[str]:
        return list(_ORDERS[customer_id])

    async with Session():
        x, y = await asyncio.gather(
            get_order_history(customer_id="c1"),
            get_order_history(customer_id="c2"),
        )

    assert x != y


# ---------------------------------------------------------------------------
# FM7 — Exceptions clear the cache entry (no regex / special HTTP handling)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm7_first_call_exception_clears_entry_then_retry_succeeds() -> None:
    """Failed call is not cached as success; next call runs the function again."""
    calls = [0]

    @protect(entity_param="x", ttl=0.05)
    async def flaky(x: str) -> str:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("429 Too Many Requests")
        return f"ok-{x}"

    async with Session() as s:
        with pytest.raises(RuntimeError):
            await flaky(x="a")
        second = await flaky(x="a")

    assert second == "ok-a"
    assert any(e["event"] == "cache_error" for e in s.audit_log())


@pytest.mark.asyncio
async def test_fm7_repeated_errors_invoke_fn_each_time() -> None:
    """Errors are not stored as cached values; each call runs the underlying function."""
    n = [0]

    @protect(entity_param="uid", ttl=60)
    async def boom(uid: str) -> str:
        n[0] += 1
        raise RuntimeError("500 Internal Server Error")

    async with Session():
        with pytest.raises(RuntimeError):
            await boom(uid="u1")
        with pytest.raises(RuntimeError):
            await boom(uid="u1")

    assert n[0] == 2


@pytest.mark.asyncio
async def test_fm7_exception_scoped_to_entity_key() -> None:
    """Exception for one entity_id does not evict another entity's cache entry."""
    calls: dict[str, int] = {"a": 0, "b": 0}

    @protect(entity_param="uid", ttl=60)
    async def rl(uid: str) -> str:
        calls[uid] = calls.get(uid, 0) + 1
        if uid == "a" and calls[uid] == 1:
            raise RuntimeError("429 rate limit")
        return uid

    async with Session():
        b_result = await rl(uid="b")
        with pytest.raises(RuntimeError):
            await rl(uid="a")
        a_result = await rl(uid="a")

    assert b_result == "b"
    assert a_result == "a"
    assert calls["b"] == 1


@pytest.mark.asyncio
async def test_fm7_second_call_succeeds_after_arbitrary_exception_message() -> None:
    """Message text is irrelevant: any exception clears the key; following call succeeds."""
    hits = [0]

    @protect(entity_param="id", ttl=60)
    async def qtool(id: str) -> str:
        hits[0] += 1
        if hits[0] == 1:
            raise RuntimeError("quota exceeded for tenant")
        return "fine"

    async with Session() as s:
        with pytest.raises(RuntimeError):
            await qtool(id="t1")
        result = await qtool(id="t1")

    assert result == "fine"
    assert any(e["event"] == "cache_error" for e in s.audit_log())


@pytest.mark.asyncio
async def test_fm7_invalidate_preserves_unrelated_tool_cache() -> None:
    """Error in 'bad' tool does not evict 'good' tool's cache entry."""
    good_calls = [0]

    @protect(ttl=60)
    async def bad() -> str:
        raise RuntimeError("429")

    @protect(ttl=60)
    async def good() -> int:
        good_calls[0] += 1
        return 42

    async with Session():
        r1 = await good()
        with pytest.raises(RuntimeError):
            await bad()
        r2 = await good()

    assert r1 == 42 and r2 == 42
    assert good_calls[0] == 1


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_long_run_scenario_completes() -> None:
    """50 tool calls (10 rounds × 3 customers + 2 SKUs) with TTL — completes without error."""

    @protect(entity_param="customer_id", ttl=0.2)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(_DB[customer_id])

    @protect(entity_param="product_id", ttl=0.2)
    async def check_inventory(product_id: str) -> dict:
        return dict(_INVENTORY[product_id])

    async with Session() as s:
        for _ in range(10):
            for cid in ("c1", "c2", "c3"):
                await fetch_customer(customer_id=cid)
            for pid in ("SKU-A", "SKU-B"):
                await check_inventory(product_id=pid)

    assert len(s.audit_log()) > 0


@pytest.mark.asyncio
async def test_integration_ttl_protect_more_backend_reads_than_never_expiring_naive() -> None:
    """Same six-call burst + sleep: naive dict reads the source once; short TTL @protect refetches."""
    state = {"val": 0}
    protect_fetches = [0]
    naive_store: dict[str, dict] = {}
    naive_fetches = [0]

    @protect(entity_param="uid", ttl=0.05)
    async def get_val(uid: str) -> dict:
        protect_fetches[0] += 1
        return {"val": state["val"]}

    async def naive_get_val(uid: str) -> dict:
        if uid in naive_store:
            return dict(naive_store[uid])
        naive_fetches[0] += 1
        r = {"val": state["val"]}
        naive_store[uid] = dict(r)
        return dict(r)

    async with Session():
        for _ in range(3):
            await get_val(uid="u1")
        await asyncio.sleep(0.11)
        for _ in range(3):
            await get_val(uid="u1")

    for _ in range(3):
        await naive_get_val("u1")
    await asyncio.sleep(0.11)
    for _ in range(3):
        await naive_get_val("u1")

    assert naive_fetches[0] == 1
    assert protect_fetches[0] > naive_fetches[0]
