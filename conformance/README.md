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
python conformance/run.py
```

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

The raw HTTP path additionally checks the unauthenticated health endpoint. CI
runs the same command, so local and hosted verification use one harness.

## What it does not prove

This suite validates the trusted-local profile only. It does not prove remote
deployment, multi-tenancy, production authentication, provider truth,
hostile-client protection, bypass prevention, or exactly-once execution.
