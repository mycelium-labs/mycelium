# Local sidecar conformance suite

This suite proves that raw HTTP, the TypeScript client, and the Go client agree
with the authoritative Python sidecar for the frozen development protocol
`v1alpha1`.

It runs entirely on loopback with temporary file storage and synthetic effects.
It never calls an application tool, LLM, or external provider.

## Run

Requirements:

- Python 3.10+ with the repository SDK installed
- Node.js 18+ and npm
- Go 1.22+

```bash
python -m pip install -e ./sdk
sdk/.venv/bin/python conformance/run.py
```

The shared PostgreSQL profile has a separate Docker-backed smoke/conformance
command. It starts one disposable PostgreSQL container and two independent
sidecar processes, then races claims through both HTTP endpoints:

```bash
sdk/.venv/bin/python conformance/run_postgres.py
```

The command requires Docker, the repository's supported `sdk/.venv`, and the
`mycelium-runtime[postgres]` extra. It uses synthetic effects only. The full
local suite remains the compatibility check for the file-backed profile.

The runner installs the pinned TypeScript development dependency when needed,
builds that client, starts an authenticated sidecar on an operating-system
assigned loopback port, runs every client, and shuts the server down.

## What it checks

Every client checks:

- compatibility with `v1alpha1`;
- the approved `identity-v1` fixture;
- authentication rejection;
- claim → provider boundary → complete → stored-result replay;
- stale-fence rejection;
- fail-closed handling of an unknown claim disposition;
- fail-closed handling of a malformed reconciliation marker;
- conservative uncertainty after a transport timeout.

The raw HTTP path additionally checks the unauthenticated health endpoint and
the two-stage `RECORD_DECISION` → `EXECUTE` flow. CI runs the same command, so
local and hosted verification use one harness.

The failure scenarios launch TypeScript and Go as separate operating-system
processes. They race for one effect, kill owners before and after the provider
boundary, wait for leases to expire, reject late owner writes, and restart the
sidecar over the same durable ledger. The trusted-local blind profile parks
crashed irreversible actions instead of granting an unsafe automatic takeover.

## What it does not prove

This suite validates the trusted-local profile only. It does not prove remote
deployment, multi-tenancy, production authentication, provider truth,
hostile-client protection, bypass prevention, or exactly-once execution. It
also does not prove reconciler-backed takeover or coordination between multiple
sidecar server processes.
