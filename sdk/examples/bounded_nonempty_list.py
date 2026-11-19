"""@bounded input boundary: item type validation is not collection length.

A field can be typed correctly and still be empty. ``type: array`` with
``min_length: 1`` rejects ``[]`` before the tool body runs, while a non-empty
list reaches the tool normally. Mycelium already ships this behaviour; the
example only shows it.

Run from the sdk directory::

    python examples/bounded_nonempty_list.py
"""

from __future__ import annotations

from mycelium.tool_boundary import ToolBoundaryError, bounded_sync

# Item typing accepts any string. Collection typing rejects zero items.
NOTIFY_SCHEMA = {
    "recipients": {
        "type": "array",
        "items": {"type": "string"},
        "required": True,
        "min_length": 1,
    },
}


def main() -> None:
    invocations = {"n": 0}

    @bounded_sync(schema=NOTIFY_SCHEMA)
    def send_notice(recipients: list[str]) -> dict[str, object]:
        invocations["n"] += 1
        return {"sent": len(recipients), "recipients": recipients}

    print("=== @bounded array length boundary ===\n")
    print(
        "type: array + items: {type: string} constrains each item's type.\n"
        "min_length: 1 additionally constrains how many items arrive.\n"
        "A list of the wrong item type fails item validation; [] fails length.\n"
    )

    print("1) Non-empty list of strings reaches the tool")
    result = send_notice(recipients=["ops@example.com", "oncall@example.com"])
    print(f"   ok: {result}; bodies={invocations['n']}")

    print("\n2) Empty list is rejected before the tool body")
    try:
        send_notice(recipients=[])
    except ToolBoundaryError as exc:
        print(f"   REFUSED violation={exc.violation}; field={exc.field}")
        print(f"   body not executed; bodies={invocations['n']}")

    print("\n3) Wrong item type is a different violation, same fail-closed result")
    try:
        send_notice(recipients=[1])
    except ToolBoundaryError as exc:
        print(f"   REFUSED violation={exc.violation}; field={exc.field}")
        print(f"   body not executed; bodies={invocations['n']}")

    assert invocations["n"] == 1, "boundary failures must not run the tool body"
    print(f"\nboundary ok: accepted=1 rejected_empty=1 rejected_type=1 bodies={invocations['n']}")


if __name__ == "__main__":
    main()
