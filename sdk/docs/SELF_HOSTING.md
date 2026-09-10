# Self-host the language-neutral sidecar

Mycelium is open-source, self-hosted software. There is no Mycelium cloud
account or hosted endpoint to create. Your application talks to a Mycelium
sidecar that you run:

```text
TypeScript, Go, Java, Rust, or raw HTTP
                    |
                 HTTP/JSON
                    |
          Python Mycelium sidecar
                    |
          durable Mycelium ledger
```

Use the local profile for one machine and trusted development. Use the shared
PostgreSQL profile when multiple sidecars or application instances must
coordinate.

## Local setup

Requirements:

- Python 3.10 or newer
- `openssl`

Install Mycelium and create a private working directory:

```bash
python3 -m pip install mycelium-runtime
export MYCELIUM_DIR="$HOME/.mycelium/sidecar"
mkdir -p "$MYCELIUM_DIR"
umask 077
openssl rand -hex 32 > "$MYCELIUM_DIR/token"
```

Create the configuration. The paths written by this command are absolute:

```bash
cat > "$MYCELIUM_DIR/sidecar.yaml" <<EOF
kind: mycelium-sidecar
profile: development
protocol_version: "v1alpha1"
identity_namespace: identity-v1
tenant_id: local
application_id: my-app
bearer_token_file: $MYCELIUM_DIR/token
ledger:
  type: file
  path: $MYCELIUM_DIR/ledger.json
outcome_storage:
  type: file
  path: $MYCELIUM_DIR/outcomes.ndjson
server:
  host: 127.0.0.1
  port: 8787
EOF
```

Start the sidecar:

```bash
mycelium sidecar serve --config "${MYCELIUM_DIR}/sidecar.yaml"
```

Leave that process running. In a second terminal, check the process and
authenticated protocol:

```bash
export MYCELIUM_SIDECAR_TOKEN="$(cat "$HOME/.mycelium/sidecar/token")"
curl --fail http://127.0.0.1:8787/health
curl --fail http://127.0.0.1:8787/v1/capabilities \
  -H "Authorization: Bearer $MYCELIUM_SIDECAR_TOKEN"
```

`/health` proves the server is running. `/ready` also checks its storage.

## Connect an application

The sidecar contract is ordinary authenticated HTTP/JSON, so an official
language package is optional.

For TypeScript or Node.js:

```bash
npm install @mycelium-labs/sidecar-client@experimental
```

Follow the [TypeScript lifecycle example](../../clients/typescript/README.md#use-the-client).

For Go:

```bash
go get github.com/mycelium-labs/mycelium/clients/go@v0.1.0
```

Follow the [Go lifecycle example](../../clients/go/README.md#client-lifecycle).

For Java, Rust, C#, Ruby, or another language, use
`GET /v1/openapi.json` with the bearer token and follow the same lifecycle:

1. Submit a stable action identity and validated decision evidence.
2. Call the provider only when the claim disposition is `EXECUTE`.
3. Record the provider boundary immediately before the provider call.
4. Complete or fail the effect with the returned owner and fence.
5. Never treat a timeout or unknown disposition as permission to execute.

The sidecar does not call the provider for you. Provider calls made without
this lifecycle bypass Mycelium.

## Shared PostgreSQL setup

Use this profile when separate processes or machines need one authoritative
answer about an action. The repository includes a two-sidecar Docker Compose
deployment.

Requirements:

- Git
- Docker with Compose
- `openssl`

From a clone of the repository:

```bash
git clone https://github.com/mycelium-labs/mycelium.git
cd mycelium
export MYCELIUM_POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export MYCELIUM_SIDECAR_TOKEN="$(openssl rand -hex 32)"
docker compose -f docker-compose.sidecar.yml up --build
```

This starts PostgreSQL and two sidecars over the same ledger:

- `http://127.0.0.1:8787`
- `http://127.0.0.1:8788`

Both endpoints use the same token, `example-tenant`, and `example-app`. Test
either endpoint:

```bash
curl --fail http://127.0.0.1:8787/ready
curl --fail http://127.0.0.1:8788/v1/capabilities \
  -H "Authorization: Bearer $MYCELIUM_SIDECAR_TOKEN"
```

Stop the deployment without deleting its PostgreSQL volume:

```bash
docker compose -f docker-compose.sidecar.yml down
```

The Compose file is a runnable self-hosting example, not a hardened
internet-facing deployment. For another container platform, use
[`Dockerfile.sidecar`](../../Dockerfile.sidecar) with
[`deploy/sidecar.shared.yaml`](../../deploy/sidecar.shared.yaml), provide
`MYCELIUM_DATABASE_URL`, and mount the bearer token at `/tmp/token`.

## Security boundary

- Keep the development profile on loopback.
- Keep shared sidecars and PostgreSQL on a private network.
- Terminate TLS at a trusted reverse proxy before allowing remote clients.
- Supply database credentials and bearer tokens through your platform's
  secret manager; never commit them or put them in URLs.
- Run one tenant/application scope per sidecar configuration. The current
  profile is not a general multi-tenant IAM service.
- Rotate bearer credentials with `bearer_token_files`, retaining the old and
  new token files only for the overlap period.

The shared profile provides PostgreSQL claim arbitration and fencing. It does
not provide exactly-once provider execution, hostile-client protection,
provider truth, or protection for calls that bypass Mycelium. Keep ambiguous
provider outcomes blocked until they can be reconciled safely.

## Verify the installation

Repository contributors can run both conformance paths:

```bash
sdk/.venv/bin/python conformance/run.py
sdk/.venv/bin/python conformance/run_postgres.py
```

The second command starts disposable PostgreSQL and two independent sidecars,
then verifies cross-sidecar ownership, TypeScript and Go compatibility, replay,
fencing, authentication, and uncertainty handling. See the
[conformance guide](../../conformance/README.md) for prerequisites and limits.

## Troubleshooting

- **The token is rejected:** use exactly 64 hexadecimal characters or 43
  base64url characters, and keep the token file owner-only.
- **The sidecar rejects the configuration:** use absolute paths in the local
  profile and PostgreSQL for both ledger and outcome storage in the shared
  profile.
- **`/health` works but `/ready` fails:** the process is running but its storage
  is unavailable or not initialized.
- **A second request returns `WAIT_FOR_OWNER`:** another client owns the active
  effect. Do not call the provider.
- **An effect returns `UNKNOWN`:** the provider operation may have happened.
  Do not retry blindly.

For protocol details, see the
[language-neutral specification](spec/README.md). For vulnerability reports,
follow the private process in [`SECURITY.md`](../../SECURITY.md).
