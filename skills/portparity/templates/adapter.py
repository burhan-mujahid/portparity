"""portparity adapter template (Python 3.8+).

Protocol: read one JSON request per stdin line, {"id": ..., "input": ...}; write exactly one
JSON response line per request, {"id": ..., "ok": true, "output": ...} or
{"id": ..., "ok": false, "error": {"type": ..., "message": ...}}.
Write logs to stderr only. The same adapter works in "process" and "stream" mode.
"""

import json
import sys


class BadInput(Exception):
    """The input does not match the component's input contract."""


# Map native exceptions to a shared error vocabulary used by BOTH sides.
# Unmapped exceptions stay visible as "unmapped:<Name>" so they cannot silently match.
ERROR_TYPES = {
    BadInput: "bad_input",
    # ValueError: "invalid_argument",
}


def call(value):
    """Translate `value` into a call to the component and return a JSON-safe result.
    Encode decimals and integers beyond 2**53 as strings; never return NaN or Infinity."""
    return value  # TODO: replace with the real call, e.g. my_module.fee(**value)


def error_type(exc):
    for cls, name in ERROR_TYPES.items():
        if isinstance(exc, cls):
            return name
    return "unmapped:" + type(exc).__name__


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        try:
            response = {"id": request["id"], "ok": True, "output": call(request["input"])}
        except Exception as exc:  # report every exception as behavior
            response = {"id": request["id"], "ok": False,
                        "error": {"type": error_type(exc), "message": str(exc)}}
        sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
