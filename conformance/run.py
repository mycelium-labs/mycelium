#!/usr/bin/env python3
"""Run the local v1alpha1 sidecar conformance suite across supported clients."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk"))

from mycelium.sidecar import SidecarConfig, build_service, serve_in_thread

TOKEN = "c" * 43
TENANT_ID = "tenant-a"
APPLICATION_ID = "app.example"


def identity(business_request_id: str) -> dict[str, Any]:
    return {
        "application_id": APPLICATION_ID,
        "business_request_id": business_request_id,
        "canonicalization_version": "jcs-1",
        "destination": {"id": "record-9", "kind": "record"},
        "execution_scope": {"entity": "record-9", "tenant": TENANT_ID},
        "identity_version": "1",
        "input": {"operation": "update", "value": "new-value"},
        "tenant_id": TENANT_ID,
        "tool_contract_version": "1",
        "tool_id": "external_operation",
    }


def request(
    base_url: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str = TOKEN,
) -> tuple[int, dict[str, Any]]:
    raw = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if raw is not None:
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(base_url + path, data=raw, headers=headers, method=method)
    try:
        with urllib.request.urlopen(call, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)


def require_status(
    response: tuple[int, dict[str, Any]], expected: int, context: str
) -> dict[str, Any]:
    status, payload = response
    if status != expected:
        raise AssertionError(f"{context}: expected HTTP {expected}, got {status}: {payload}")
    return payload


def raw_http_checks(base_url: str, expected_effect_id: str) -> list[str]:
    checks: list[str] = []

    health = require_status(request(base_url, "GET", "/health", token=""), 200, "health")
    assert health == {"status": "ok", "protocol_version": "v1alpha1"}
    checks.append("health")

    missing_auth = require_status(
        request(base_url, "GET", "/v1/capabilities", token=""),
        401,
        "missing authentication",
    )
    assert missing_auth["error"]["code"] == "AUTHENTICATION_REQUIRED"
    checks.append("authentication")

    capabilities = require_status(
        request(base_url, "GET", "/v1/capabilities"), 200, "capabilities"
    )
    assert capabilities["protocol_version"] == "v1alpha1"
    assert capabilities["identity_namespace"] == "identity-v1"
    assert capabilities["development_only"] is True
    checks.append("capabilities")

    derived = require_status(
        request(
            base_url,
            "POST",
            "/v1/identities/derive",
            body=identity("request-884"),
        ),
        200,
        "identity fixture",
    )
    assert derived["effect_id"] == expected_effect_id
    checks.append("identity-fixture")

    decision_candidate = identity("conformance-raw-two-stage-decision")
    intended = require_status(
        request(
            base_url,
            "POST",
            "/v1/effects/claim",
            body=decision_candidate,
        ),
        200,
        "decisionless claim",
    )
    assert intended["disposition"] == "RECORD_DECISION"
    decided = require_status(
        request(
            base_url,
            "POST",
            "/v1/effects/claim",
            body={
                **decision_candidate,
                "decision": {"allowed": True, "verdicts": [], "denied_reasons": []},
            },
        ),
        200,
        "decision recording claim",
    )
    assert decided["disposition"] == "EXECUTE"
    require_status(
        request(
            base_url,
            "POST",
            f"/v1/effects/{decided['effect_id']}/complete",
            body={
                **decision_candidate,
                "owner_id": decided["owner_id"],
                "fence": decided["fence"],
                "result": {"decision": "recorded"},
            },
        ),
        200,
        "decision lifecycle completion",
    )
    checks.append("two-stage-decision")

    candidate = identity("conformance-raw-lifecycle")
    claim = require_status(
        request(
            base_url,
            "POST",
            "/v1/effects/claim",
            body={
                **candidate,
                "decision": {"allowed": True, "verdicts": [], "denied_reasons": []},
            },
        ),
        200,
        "initial claim",
    )
    assert claim["disposition"] == "EXECUTE"
    handle = {"owner_id": claim["owner_id"], "fence": claim["fence"]}
    require_status(
        request(
            base_url,
            "POST",
            f"/v1/effects/{claim['effect_id']}/boundary",
            body={**candidate, **handle, "boundary": "maybe_crossed"},
        ),
        200,
        "provider boundary",
    )
    completed = require_status(
        request(
            base_url,
            "POST",
            f"/v1/effects/{claim['effect_id']}/complete",
            body={**candidate, **handle, "result": {"client": "raw-http"}},
        ),
        200,
        "completion",
    )
    assert completed["effect_state"] == "COMMITTED"
    replay = require_status(
        request(
            base_url,
            "POST",
            "/v1/effects/claim",
            body={
                **candidate,
                "decision": {"allowed": True, "verdicts": [], "denied_reasons": []},
            },
        ),
        200,
        "replay",
    )
    assert replay["disposition"] == "RETURN_STORED_RESULT"
    assert replay["result"] == {"client": "raw-http"}
    checks.append("claim-complete-replay")

    stale_candidate = identity("conformance-raw-stale-fence")
    stale_claim = require_status(
        request(
            base_url,
            "POST",
            "/v1/effects/claim",
            body={
                **stale_candidate,
                "decision": {"allowed": True, "verdicts": [], "denied_reasons": []},
            },
        ),
        200,
        "stale-fence claim",
    )
    stale = require_status(
        request(
            base_url,
            "POST",
            f"/v1/effects/{stale_claim['effect_id']}/complete",
            body={
                **stale_candidate,
                "owner_id": stale_claim["owner_id"],
                "fence": stale_claim["fence"] + 1,
                "result": {"unsafe": True},
            },
        ),
        409,
        "stale-fence rejection",
    )
    assert stale["error"]["code"] == "STALE_FENCE"
    assert stale["error"]["state_may_have_changed"] is True
    checks.append("stale-fence")

    return checks


def run_client(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    client: str,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(
            f"{client} conformance failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"{client} conformance produced no result")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{client} conformance ended with non-JSON output:\n{completed.stdout}"
        ) from exc
    if result.get("client") != client:
        raise RuntimeError(f"{client} conformance returned the wrong client label: {result}")
    return result


def driver_environment(
    base: dict[str, str],
    request_id: str,
    *,
    lease_ttl: float,
    hold_ms: int = 0,
    boundary: str | None = None,
    start_at_ms: int | None = None,
) -> dict[str, str]:
    environment = {
        **base,
        "MYCELIUM_CONFORMANCE_REQUEST_ID": request_id,
        "MYCELIUM_CONFORMANCE_LEASE_TTL": str(lease_ttl),
        "MYCELIUM_CONFORMANCE_HOLD_MS": str(hold_ms),
    }
    if boundary is not None:
        environment["MYCELIUM_CONFORMANCE_BOUNDARY"] = boundary
    if start_at_ms is not None:
        environment["MYCELIUM_CONFORMANCE_START_AT_MS"] = str(start_at_ms)
    return environment


def start_crash_driver(
    client: str,
    environment: dict[str, str],
    go_driver: Path,
) -> subprocess.Popen[str]:
    if client == "typescript":
        command = ["node", "crash-conformance.mjs"]
        cwd = ROOT / "clients/typescript"
    elif client == "go":
        command = [str(go_driver)]
        cwd = ROOT / "clients/go"
    else:
        raise ValueError(f"unknown crash driver: {client}")
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def read_crash_driver(
    process: subprocess.Popen[str],
    client: str,
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError(f"{client} crash driver has no output pipes")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        if not selector.select(timeout):
            raise RuntimeError(f"{client} crash driver did not become ready")
        line = process.stdout.readline()
    finally:
        selector.close()
    if not line:
        stderr = process.stderr.read()
        raise RuntimeError(
            f"{client} crash driver exited before reporting a claim "
            f"(status={process.poll()}):\n{stderr}"
        )
    try:
        result = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{client} crash driver returned invalid JSON: {line}") from exc
    if result.get("client") != client:
        raise RuntimeError(f"{client} crash driver returned the wrong label: {result}")
    return result


def stop_process(process: subprocess.Popen[str], *, crash: bool) -> None:
    if process.poll() is not None:
        return
    if crash:
        process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_crash_driver(
    client: str,
    environment: dict[str, str],
    go_driver: Path,
) -> dict[str, Any]:
    process = start_crash_driver(client, environment, go_driver)
    try:
        result = read_crash_driver(process, client)
        status = process.wait(timeout=5)
        if status:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"{client} crash driver failed ({status}):\n{stderr}")
        return result
    finally:
        stop_process(process, crash=False)


def assert_old_owner_rejected(
    base_url: str,
    candidate: dict[str, Any],
    owner: dict[str, Any],
    *,
    context: str,
    expected_state: str,
) -> None:
    status, payload = request(
        base_url,
        "POST",
        f"/v1/effects/{owner['effect_id']}/complete",
        body={
            **candidate,
            "owner_id": owner["owner_id"],
            "fence": owner["fence"],
            "result": {"unsafe": True},
        },
    )
    if status != 409:
        raise AssertionError(f"{context}: old owner completion was not rejected: {status} {payload}")
    if payload["error"]["code"] not in {"STALE_FENCE", "LEASE_LOST", "INVALID_TRANSITION"}:
        raise AssertionError(f"{context}: unexpected rejection: {payload}")
    inspected = require_status(
        request(base_url, "GET", f"/v1/effects/{owner['effect_id']}"),
        200,
        f"{context} inspection",
    )
    assert inspected["effect_state"] == expected_state
    assert inspected["result"] is None


def crash_and_concurrency_checks(
    base_url: str,
    environment: dict[str, str],
    go_driver: Path,
) -> tuple[list[str], str]:
    checks: list[str] = []
    decision = {"allowed": True, "verdicts": [], "denied_reasons": []}

    race_request_id = "conformance-cross-language-race"
    start_at_ms = int(time.time() * 1000) + 500
    race_environment = driver_environment(
        environment,
        race_request_id,
        lease_ttl=5,
        hold_ms=30_000,
        start_at_ms=start_at_ms,
    )
    race_processes = {
        client: start_crash_driver(client, race_environment, go_driver)
        for client in ("typescript", "go")
    }
    try:
        race_results = {
            client: read_crash_driver(process, client)
            for client, process in race_processes.items()
        }
        dispositions = sorted(result["disposition"] for result in race_results.values())
        assert dispositions == ["EXECUTE", "WAIT_FOR_OWNER"], race_results
        owner_client = next(
            client
            for client, result in race_results.items()
            if result["disposition"] == "EXECUTE"
        )
        waiter_client = "go" if owner_client == "typescript" else "typescript"
        waiter_status = race_processes[waiter_client].wait(timeout=5)
        if waiter_status:
            raise AssertionError(f"{waiter_client} waiter failed with {waiter_status}")
        owner = race_results[owner_client]
        candidate = identity(race_request_id)
        completed = require_status(
            request(
                base_url,
                "POST",
                f"/v1/effects/{owner['effect_id']}/complete",
                body={
                    **candidate,
                    "owner_id": owner["owner_id"],
                    "fence": owner["fence"],
                    "result": {"scenario": "cross-language-race"},
                },
            ),
            200,
            "concurrent owner completion",
        )
        assert completed["effect_state"] == "COMMITTED"
        checks.append("cross-language-single-owner")
    finally:
        for process in race_processes.values():
            stop_process(process, crash=False)

    pre_request_id = "conformance-typescript-crash-before-boundary"
    pre_candidate = identity(pre_request_id)
    pre_owner_process = start_crash_driver(
        "typescript",
        driver_environment(
            environment, pre_request_id, lease_ttl=1, hold_ms=30_000
        ),
        go_driver,
    )
    try:
        pre_owner = read_crash_driver(pre_owner_process, "typescript")
        assert pre_owner["disposition"] == "EXECUTE"
        stop_process(pre_owner_process, crash=True)
    finally:
        stop_process(pre_owner_process, crash=True)
    time.sleep(1.3)
    pre_recovery = run_crash_driver(
        "go",
        driver_environment(environment, pre_request_id, lease_ttl=1),
        go_driver,
    )
    assert pre_recovery["disposition"] == "TERMINAL_ABORTED", pre_recovery
    assert_old_owner_rejected(
        base_url,
        pre_candidate,
        pre_owner,
        context="pre-boundary crash",
        expected_state="ABORTED",
    )
    checks.append("pre-boundary-crash-parks")

    post_request_id = "conformance-go-crash-after-boundary"
    post_candidate = identity(post_request_id)
    post_owner_process = start_crash_driver(
        "go",
        driver_environment(
            environment,
            post_request_id,
            lease_ttl=1,
            hold_ms=30_000,
            boundary="maybe_crossed",
        ),
        go_driver,
    )
    try:
        post_owner = read_crash_driver(post_owner_process, "go")
        assert post_owner["disposition"] == "EXECUTE"
        stop_process(post_owner_process, crash=True)
    finally:
        stop_process(post_owner_process, crash=True)
    time.sleep(1.3)
    post_recovery = run_crash_driver(
        "typescript",
        driver_environment(environment, post_request_id, lease_ttl=1),
        go_driver,
    )
    assert post_recovery["disposition"] == "UNKNOWN", post_recovery
    assert_old_owner_rejected(
        base_url,
        post_candidate,
        post_owner,
        context="post-boundary crash",
        expected_state="UNKNOWN",
    )
    checks.append("post-boundary-crash-parks")

    race_replay = require_status(
        request(
            base_url,
            "POST",
            "/v1/effects/claim",
            body={**identity(race_request_id), "decision": decision},
        ),
        200,
        "pre-restart stored result",
    )
    assert race_replay["disposition"] == "RETURN_STORED_RESULT"
    return checks, race_request_id


def main() -> int:
    for executable in ("node", "npm", "go"):
        if shutil.which(executable) is None:
            raise SystemExit(f"required executable is unavailable: {executable}")

    fixture = json.loads(
        (ROOT / "sdk/docs/spec/fixtures/effect-identity.json").read_text(encoding="utf-8")
    )
    expected_effect_id = str(fixture["base_case"]["expected_effect_id"])

    with tempfile.TemporaryDirectory(prefix="mycelium-conformance-") as directory:
        temp = Path(directory)
        token_path = temp / "sidecar.token"
        token_path.write_text(TOKEN + "\n", encoding="ascii")
        token_path.chmod(0o600)
        config_path = temp / "sidecar.yaml"
        config_path.write_text(
            "\n".join(
                (
                    "kind: mycelium-sidecar",
                    'protocol_version: "v1alpha1"',
                    "identity_namespace: identity-v1",
                    f"tenant_id: {TENANT_ID}",
                    f"application_id: {APPLICATION_ID}",
                    f"bearer_token_file: {token_path}",
                    f"ledger: {{type: file, path: {temp / 'ledger.json'}}}",
                    f"outcome_storage: {{type: file, path: {temp / 'outcomes.ndjson'}}}",
                    "server: {host: 127.0.0.1, port: 0}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        config = SidecarConfig.from_yaml(config_path)
        go_driver = temp / "go-crash-conformance"
        subprocess.run(
            ["go", "build", "-o", str(go_driver), "./cmd/crash-conformance"],
            cwd=ROOT / "clients/go",
            check=True,
        )
        server, thread = serve_in_thread(build_service(config))
        base_url = f"http://127.0.0.1:{server.server_port}"
        environment = {
            **os.environ,
            "MYCELIUM_CONFORMANCE_URL": base_url,
            "MYCELIUM_CONFORMANCE_TOKEN": TOKEN,
            "MYCELIUM_CONFORMANCE_TENANT": TENANT_ID,
            "MYCELIUM_CONFORMANCE_APPLICATION": APPLICATION_ID,
            "MYCELIUM_CONFORMANCE_EFFECT_ID": expected_effect_id,
        }
        try:
            raw_checks = raw_http_checks(base_url, expected_effect_id)

            typescript = ROOT / "clients/typescript"
            if not (typescript / "node_modules/.bin/tsc").exists():
                subprocess.run(["npm", "ci"], cwd=typescript, check=True, env=environment)
            subprocess.run(["npm", "run", "build"], cwd=typescript, check=True, env=environment)
            ts_result = run_client(
                ["node", "conformance.mjs"],
                cwd=typescript,
                environment=environment,
                client="typescript",
            )

            go_result = run_client(
                ["go", "run", "./cmd/conformance"],
                cwd=ROOT / "clients/go",
                environment=environment,
                client="go",
            )
            failure_checks, restart_request_id = crash_and_concurrency_checks(
                base_url, environment, go_driver
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        restarted_server, restarted_thread = serve_in_thread(build_service(config))
        restarted_environment = {
            **environment,
            "MYCELIUM_CONFORMANCE_URL": (
                f"http://127.0.0.1:{restarted_server.server_port}"
            ),
        }
        try:
            restart_results = [
                run_crash_driver(
                    client,
                    driver_environment(
                        restarted_environment, restart_request_id, lease_ttl=1
                    ),
                    go_driver,
                )
                for client in ("typescript", "go")
            ]
            for result in restart_results:
                assert result["disposition"] == "RETURN_STORED_RESULT", result
                assert result["result"] == {"scenario": "cross-language-race"}, result
            failure_checks.append("sidecar-restart-stored-result")
        finally:
            restarted_server.shutdown()
            restarted_server.server_close()
            restarted_thread.join(timeout=5)

    for result in (ts_result, go_result):
        if result.get("effect_id") != expected_effect_id:
            raise AssertionError(f"{result['client']} returned a different identity: {result}")

    print(
        json.dumps(
            {
                "protocol": "v1alpha1",
                "status": "passed",
                "clients": {
                    "raw-http": raw_checks,
                    "typescript": ts_result["checks"],
                    "go": go_result["checks"],
                    "cross-language-failures": failure_checks,
                },
                "effect_id": expected_effect_id,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
