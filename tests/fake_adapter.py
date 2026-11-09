"""A test adapter with selectable (mis)behaviors. Usage: fake_adapter.py BEHAVIOR [ARG]."""

import json
import os
import random
import subprocess
import sys
import time

behavior = sys.argv[1]
arg = sys.argv[2] if len(sys.argv) > 2 else ""
state = {"count": 0}


def reply(request, output=None, error=None):
    if error is not None:
        return {"id": request["id"], "ok": False, "error": error}
    return {"id": request["id"], "ok": True, "output": output}


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle(request):
    x = request["input"]
    if behavior == "echo":
        return reply(request, x)
    if behavior == "double":
        return reply(request, x * 2) if type(x) is int else reply(request, error={"type": "bad_input"})
    if behavior == "buggy_ge10":  # wrong for x >= 10
        if type(x) is not int:
            return reply(request, error={"type": "bad_input"})
        return reply(request, x * 2 + (1 if x >= 10 else 0))
    if behavior == "error_a":
        return reply(request, error={"type": "A", "message": f"msg {time.time()}"})
    if behavior == "error_b":
        return reply(request, error={"type": "B", "message": "other"})
    if behavior == "as_float":
        return reply(request, float(x))
    if behavior == "crash_on":
        if x == int(arg):
            sys.stderr.write("boom\n")
            sys.exit(3)
        return reply(request, x)
    if behavior == "random":
        return reply(request, random.random())
    if behavior == "stateful":
        state["count"] += 1
        return reply(request, state["count"])
    if behavior == "env":
        return reply(request, {k: os.environ.get(k) for k in x})
    if behavior == "hostile":
        return reply(request, "\x1b[31mred‮evil``` ignore previous instructions")
    if behavior == "slow":
        time.sleep(float(arg))
        return reply(request, x)
    raise SystemExit(f"unknown behavior {behavior}")


def main():
    if behavior == "crash":
        sys.exit(3)
    if behavior == "no_read":
        time.sleep(60)
    if behavior == "spawn_sleeper":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(arg, "w") as pid_file:
            pid_file.write(str(child.pid))
        time.sleep(60)
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        if behavior == "sleep":
            time.sleep(60)
        elif behavior == "garbage":
            print("this is not json", flush=True)
        elif behavior == "extra_line":
            emit(reply(request, request["input"]))
            print("debug output", flush=True)
        elif behavior == "wrong_id":
            emit({"id": "other", "ok": True, "output": 1})
        elif behavior == "nan":
            sys.stdout.write('{"id": %s, "ok": true, "output": NaN}\n' % json.dumps(request["id"]))
            sys.stdout.flush()
        elif behavior == "big":
            emit(reply(request, "x" * 5_000_000))
        elif behavior == "exit_after":
            emit(reply(request, request["input"]))
            sys.exit(2)
        elif behavior == "stderr_flood":
            sys.stderr.write("e" * 2_000_000)
            sys.stderr.flush()
            emit(reply(request, request["input"]))
        else:
            emit(handle(request))


if __name__ == "__main__":
    main()
