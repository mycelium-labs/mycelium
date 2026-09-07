"""Local LangGraph-shaped email workflow using the normal Mycelium wrapper."""
from pathlib import Path
from uuid import uuid4
from mycelium.config import load_config

CONFIG = Path(__file__).with_name("mycelium.yaml")
sent: list[dict[str, str]] = []

def sandbox_provider(*, to: str, subject: str, body: str, idempotency_key: str = "") -> dict[str, str]:
    sent.append({"to": to, "subject": subject, "idempotency_key": idempotency_key})
    return {"provider": "local-sandbox", "status": "accepted", "to": to}

def main() -> None:
    config = load_config(CONFIG)
    send_email = config.apply_tool("send_email", sandbox_provider)
    request_id = "email-order-" + uuid4().hex[:10]
    result = send_email(to="alice@example.test", subject="Hello", body="Welcome", idempotency_key=request_id, request_id=request_id)
    print("simulated execution:", result)
    print("sandbox provider calls:", len(sent))
    print("inspect outcome with: mycelium transitions list -c", CONFIG)
    print("simulated interruption: provider call may have crossed the boundary; outcome=UNKNOWN")
    print("supported recovery: provider reconciliation or operator release, then inspect the resolved record")
    print("uncertain outcome: reconcile the durable transition before any retry; UNKNOWN is not safe to retry.")

if __name__ == "__main__":
    main()
