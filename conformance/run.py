#!/usr/bin/env python3
"""Run the local v1alpha1 sidecar conformance suite across supported clients."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

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
                },
                "effect_id": expected_effect_id,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
