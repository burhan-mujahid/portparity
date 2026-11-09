"""portparity adapter for the Python port: one JSON request per stdin line,
one JSON response per stdout line. Diagnostics go to stderr only."""

import json
import sys

from shipping import InvalidArgument, UnknownZone, fee


class BadInput(Exception):
    pass


ERROR_TYPES = {BadInput: "bad_input", InvalidArgument: "invalid_argument", UnknownZone: "unknown_zone"}


def parse(value):
    if not isinstance(value, dict):
        raise BadInput("input must be an object")
    weight = value.get("weight_grams")
    if type(weight) is not int or not -(2**31) <= weight < 2**31:
        raise BadInput("weight_grams must be a 32-bit integer")
    zone = value.get("zone")
    if zone is not None and type(zone) is not str:
        raise BadInput("zone must be a string or null")
    express = value.get("express")
    if type(express) is not bool:
        raise BadInput("express must be a boolean")
    return weight, zone, express


def handle(request):
    try:
        result = fee(*parse(request["input"]))
        return {"id": request["id"], "ok": True, "output": {"fee": f"{result:.2f}"}}
    except Exception as exc:  # every exception becomes a reported error
        kind = next((name for cls, name in ERROR_TYPES.items() if isinstance(exc, cls)),
                    "unmapped:" + type(exc).__name__)
        return {"id": request["id"], "ok": False, "error": {"type": kind, "message": str(exc)}}


def main():
    for line in sys.stdin:
        if line.strip():
            response = handle(json.loads(line))
            sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
