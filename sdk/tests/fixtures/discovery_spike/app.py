"""An existing application body launched with or without Mycelium."""

import json

from tools import send_notice


def run() -> None:
    first = send_notice("person@example.test", request_id="notice-1")
    second = send_notice("person@example.test", request_id="notice-1")
    print(json.dumps({"first": first, "second": second}))


if __name__ == "__main__":
    run()
