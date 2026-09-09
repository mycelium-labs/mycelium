"""Run a synthetic two-sidecar PostgreSQL claim race using Docker."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "a" * 43


def request(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_for_postgres(container: str, port: str) -> None:
    """Wait for PostgreSQL itself, then verify the host-published DSN path."""
    delay = 0.25
    last_probe = ""
    for _ in range(40):
        probe = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", "postgres", "-d", "mycelium"],
            check=False, capture_output=True, text=True,
        )
        last_probe = (probe.stdout + probe.stderr).strip()
        if probe.returncode == 0:
            return
        time.sleep(delay)
        delay = min(delay * 1.35, 2.0)
    status = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", container],
        check=False, capture_output=True, text=True,
    ).stdout.strip()
    logs = subprocess.run(
        ["docker", "logs", "--tail", "80", container],
        check=False, capture_output=True, text=True,
    )
    raise RuntimeError(
        f"PostgreSQL did not become ready (container={status!r}, port={port}, "
        f"pg_isready={last_probe!r}). Logs:\n{logs.stdout[-4000:]}{logs.stderr[-1000:]}"
    )


def main() -> int:
    if shutil.which("docker") is None:
        raise SystemExit("docker is required; use python conformance/run.py for local conformance")
    python = ROOT / "sdk/.venv/bin/python"
    if not python.exists():
        raise SystemExit("sdk/.venv/bin/python is required")
    driver = subprocess.run(
        [str(python), "-c", "import psycopg, psycopg_pool"],
        check=False, capture_output=True, text=True,
    )
    if driver.returncode:
        raise SystemExit(
            "PostgreSQL conformance requires psycopg and psycopg_pool in sdk/.venv; "
            f"driver check failed: {driver.stderr.strip()}"
        )
    daemon = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if daemon.returncode:
        raise SystemExit("Docker daemon is unavailable; start Docker Desktop and retry")
    with tempfile.TemporaryDirectory(prefix="mycelium-pg-conformance-") as raw_dir:
        directory = Path(raw_dir)
        db_result = subprocess.run(
            ["docker", "run", "-d", "--rm", "-e", "POSTGRES_PASSWORD=postgres",
             "-e", "POSTGRES_DB=mycelium", "-p", "127.0.0.1::5432", "postgres:16-alpine"],
            check=False, capture_output=True, text=True,
        )
        if db_result.returncode:
            raise SystemExit(f"could not start PostgreSQL container: {db_result.stderr.strip()}")
        db = db_result.stdout.strip()
        processes: list[subprocess.Popen[str]] = []
        try:
            port = subprocess.run(
                ["docker", "port", db, "5432/tcp"], check=True, capture_output=True, text=True
            ).stdout.rsplit(":", 1)[-1].strip()
            wait_for_postgres(db, port)
            dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/mycelium"
            urls: list[str] = []
            for index in (1, 2):
                token = directory / f"token-{index}"
                token.write_text(TOKEN + "\n", encoding="ascii")
                token.chmod(0o600)
                config = directory / f"sidecar-{index}.yaml"
                config.write_text("\n".join([
                    "kind: mycelium-sidecar", "profile: shared", 'protocol_version: "v1alpha1"',
                    "identity_namespace: identity-v1", "tenant_id: tenant-a",
                    "application_id: app.example", f"bearer_token_file: {token}",
                    f"ledger: {{type: postgres, url: {dsn}}}",
                    f"outcome_storage: {{type: postgres, url: {dsn}}}",
                    "server: {host: 127.0.0.1, port: 0}", "",
                ]), encoding="utf-8")
                process = subprocess.Popen(
                    [str(python), "-m", "mycelium", "sidecar", "serve", "--config", str(config)],
                    cwd=ROOT / "sdk", env={**os.environ, "PYTHONPATH": str(ROOT / "sdk")},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                processes.append(process)
                line = process.stdout.readline() if process.stdout else ""
                if not line:
                    raise RuntimeError(process.stderr.read() if process.stderr else "sidecar exited")
                sidecar_port = line.rsplit(":", 1)[-1].split()[0]
                urls.append(f"http://127.0.0.1:{sidecar_port}")
            for url in urls:
                for _ in range(60):
                    if request(f"{url}/ready")[0] == 200:
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError(f"sidecar not ready: {url}")
            identity = {
                "tenant_id": "tenant-a", "application_id": "app.example",
                "business_request_id": "postgres-race-" + uuid.uuid4().hex,
                "canonicalization_version": "jcs-1", "destination": None,
                "execution_scope": {}, "identity_version": "1", "input": {"synthetic": True},
                "tool_contract_version": "1", "tool_id": "synthetic",
                "decision": {"allowed": True, "verdicts": [], "denied_reasons": []},
            }
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda url: request(url + "/v1/effects/claim", "POST", identity), urls))
            dispositions = sorted(str(result[1].get("disposition")) for result in results)
            if dispositions != ["EXECUTE", "WAIT_FOR_OWNER"]:
                raise AssertionError(results)
            fixture = json.loads(
                (ROOT / "sdk/docs/spec/fixtures/effect-identity.json").read_text(encoding="utf-8")
            )
            client_environment = {
                **os.environ,
                "MYCELIUM_CONFORMANCE_TOKEN": TOKEN,
                "MYCELIUM_CONFORMANCE_TENANT": "tenant-a",
                "MYCELIUM_CONFORMANCE_APPLICATION": "app.example",
                "MYCELIUM_CONFORMANCE_EFFECT_ID": fixture["base_case"]["expected_effect_id"],
            }
            client_environment["MYCELIUM_CONFORMANCE_URL"] = urls[0]
            subprocess.run(
                ["node", "conformance.mjs"], cwd=ROOT / "clients/typescript",
                env=client_environment, check=True,
            )
            client_environment["MYCELIUM_CONFORMANCE_URL"] = urls[1]
            subprocess.run(
                ["go", "run", "./cmd/conformance"], cwd=ROOT / "clients/go",
                env=client_environment, check=True,
            )
            print(json.dumps({
                "status": "passed", "sidecars": urls, "dispositions": dispositions,
                "clients": ["raw-http", "typescript", "go"],
            }))
            return 0
        finally:
            for process in processes:
                process.terminate()
                process.wait(timeout=5)
            subprocess.run(["docker", "stop", db], check=False, capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
