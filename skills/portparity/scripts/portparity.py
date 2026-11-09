#!/usr/bin/env python3
"""portparity: differential behavior testing for code migrations.

Runs an original implementation (the oracle) and a replacement on the same
JSON cases, compares their observable results, shrinks mismatches into small
regression cases, and writes an evidence report.

Passing cases are evidence for the tested inputs only. They do not prove that
two implementations are equivalent on every input.

Python 3.10+ standard library only. Linux and macOS are supported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import queue
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

VERSION = "0.1.0"
REPORT_SCHEMA_VERSION = 1
CONFIG_VERSION = 1
MARKER_FILE = ".portparity-run"

EXIT_PASS = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2
EXIT_INCONCLUSIVE = 3
EXIT_INEFFECTIVE = 4
EXIT_INTERRUPTED = 130

BEHAVIOR_STATUSES = ("ok", "error")
FAILURE_STATUSES = ("crash", "timeout", "protocol_error", "output_limit", "unstable")

DEFAULT_LIMITS = {
    "max_case_bytes": 1 << 20,
    "max_output_bytes": 1 << 20,
    "max_stderr_bytes": 64 << 10,
    "max_cases": 100_000,
    "max_depth": 64,
}
LIMIT_CEILINGS = {
    "max_case_bytes": 64 << 20,
    "max_output_bytes": 64 << 20,
    "max_stderr_bytes": 8 << 20,
    "max_cases": 10_000_000,
    "max_depth": 512,
}
# Variables copied from the parent environment unless the config overrides them.
BASE_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT")
# Applied to both sides for reproducibility; the config may override them.
DETERMINISM_ENV = {"TZ": "UTC", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "PYTHONUTF8": "1"}

CASE_KEYS = {"id", "input", "tags", "note", "source"}
SIDE_KEYS = {"cmd", "cwd", "env", "mode"}
CONFIG_KEYS = {
    "version", "description", "original", "replacement", "cases", "timeout_seconds",
    "determinism_runs", "limits", "compare", "env", "env_passthrough",
    "negative_controls", "accepted_differences",
}
COMPARE_KEYS = {"error_fields", "float_abs_tol", "float_rel_tol", "int_float_equal"}
ACCEPTED_KEYS = {"case", "reason", "approved_by", "replacement"}
CONTROL_KEYS = {"name"} | SIDE_KEYS

LIMITATIONS = [
    "Results cover only the cases listed in this report. They do not prove equivalence on other inputs.",
    "Only behavior visible through the adapters (outputs and declared errors) is compared. "
    "Side effects, performance, concurrency, and resource use are not compared unless the adapters expose them.",
    "The original is treated as the oracle. Behavior the owner wants changed must be recorded as an "
    "accepted difference rather than reproduced.",
]


class UsageError(Exception):
    """Invalid configuration, input, or command line. Exit code 2."""


# ---------------------------------------------------------------------------
# Strict JSON


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"number out of range: {text[:40]}")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate object key {key[:80]!r}")
        obj[key] = value
    return obj


def _check_tree(value: Any, limit: int) -> None:
    """Enforce the depth limit and reject lone surrogates (I-JSON, RFC 7493)."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            raise ValueError(f"nesting deeper than {limit}")
        if isinstance(item, str):
            _check_str(item)
        elif isinstance(item, dict):
            for key in item:
                _check_str(key)
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)


def _check_str(text: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("string contains a lone surrogate (invalid Unicode)") from None


def strict_loads(text: str, max_depth: int = DEFAULT_LIMITS["max_depth"]) -> Any:
    """Parse RFC 8259 JSON. Rejects NaN/Infinity, overflow, duplicate keys and deep nesting."""
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
            object_pairs_hook=_no_duplicate_keys,
        )
    except RecursionError:
        raise ValueError(f"nesting deeper than {max_depth}") from None
    _check_tree(value, max_depth)
    return value


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def wire(value: Any) -> str:
    """Canonical ASCII-only JSON. Used for requests and JSONL files: escaping all non-ASCII
    characters keeps U+2028/U+2029 and encoding differences from splitting or corrupting lines."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def input_hash(value: Any) -> str:
    return "h-" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:16]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Configuration


@dataclass
class Side:
    name: str
    cmd: list[str]
    cwd: Path
    env: dict[str, str]
    mode: str = "process"

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "cmd": self.cmd, "cwd": str(self.cwd), "mode": self.mode,
                "env_keys": sorted(self.env)}


@dataclass
class CompareOptions:
    error_fields: tuple[str, ...] = ("type",)
    float_abs_tol: float = 0.0
    float_rel_tol: float = 0.0
    int_float_equal: bool = False


@dataclass
class Case:
    id: str
    input: Any
    source: str
    tags: list[str] = field(default_factory=list)


@dataclass
class Accepted:
    case: str
    reason: str
    approved_by: str
    replacement: dict[str, Any] | None


@dataclass
class Config:
    path: Path
    sha256: str
    description: str
    original: Side
    replacement: Side
    case_files: list[Path]
    timeout: float
    determinism_runs: int
    limits: dict[str, int]
    compare: CompareOptions
    controls: list[Side]
    accepted: dict[str, Accepted]
    env_report: dict[str, Any]


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise UsageError(message)


def _check_keys(obj: Any, allowed: set[str], where: str, required: tuple[str, ...] = ()) -> None:
    _require(isinstance(obj, dict), f"{where} must be a JSON object")
    unknown = sorted(set(obj) - allowed)
    _require(not unknown, f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")
    for key in required:
        _require(key in obj, f"{where}: missing required key {key!r}")


def _str_map(obj: Any, where: str) -> dict[str, str]:
    _require(isinstance(obj, dict), f"{where} must be an object of strings")
    for key, value in obj.items():
        _require(isinstance(value, str), f"{where}.{key} must be a string")
        _require(key and "=" not in key and "\0" not in key and "\0" not in value,
                 f"{where}.{key}: invalid environment entry")
    return dict(obj)


def _number(obj: dict, key: str, default: float, lo: float, hi: float, where: str) -> float:
    value = obj.get(key, default)
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
             f"{where}.{key} must be a number")
    _require(lo <= value <= hi, f"{where}.{key} must be between {lo} and {hi}")
    return float(value)


def _side(raw: Any, name: str, base: Path, shared_env: dict[str, str], allowed: set[str]) -> Side:
    _check_keys(raw, allowed, name, required=("cmd",))
    cmd = raw["cmd"]
    _require(isinstance(cmd, list) and cmd and all(isinstance(a, str) and a for a in cmd),
             f"{name}.cmd must be a non-empty array of strings (no shell strings; arguments are never "
             "passed through a shell)")
    _require(all("\0" not in a for a in cmd), f"{name}.cmd contains a NUL byte")
    cwd_raw = raw.get("cwd", ".")
    _require(isinstance(cwd_raw, str), f"{name}.cwd must be a string")
    cwd = (base / cwd_raw).resolve()
    mode = raw.get("mode", "process")
    _require(mode in ("process", "stream"), f"{name}.mode must be 'process' or 'stream'")
    env = dict(shared_env)
    env.update(_str_map(raw.get("env", {}), f"{name}.env"))
    return Side(name=name, cmd=list(cmd), cwd=cwd, env=env, mode=mode)


def build_env(passthrough: list[str], overrides: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in (*BASE_PASSTHROUGH, *passthrough) if k in os.environ}
    env.update(DETERMINISM_ENV)
    env.update(overrides)
    return env


def load_config(path: str | Path) -> Config:
    cfg_path = Path(path).resolve()
    _require(cfg_path.is_file(), f"config not found: {path}")
    try:
        text = cfg_path.read_text(encoding="utf-8")
        raw = strict_loads(text)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise UsageError(f"cannot read config {path}: {exc}") from None
    _check_keys(raw, CONFIG_KEYS, "config", required=("version", "original", "replacement", "cases"))
    _require(raw["version"] == CONFIG_VERSION, f"config.version must be {CONFIG_VERSION}")
    base = cfg_path.parent

    passthrough = raw.get("env_passthrough", [])
    _require(isinstance(passthrough, list) and all(isinstance(v, str) and v for v in passthrough),
             "config.env_passthrough must be an array of variable names")
    shared_overrides = _str_map(raw.get("env", {}), "config.env")
    shared_env = build_env(passthrough, shared_overrides)

    original = _side(raw["original"], "original", base, shared_env, SIDE_KEYS)
    replacement = _side(raw["replacement"], "replacement", base, shared_env, SIDE_KEYS)

    cases = raw["cases"]
    _require(isinstance(cases, list) and cases and all(isinstance(c, str) and c for c in cases),
             "config.cases must be a non-empty array of JSONL file paths")
    case_files = [(base / c).resolve() for c in cases]

    timeout = _number(raw, "timeout_seconds", 10, 0.05, 3600, "config")
    runs = raw.get("determinism_runs", 2)
    _require(isinstance(runs, int) and not isinstance(runs, bool) and 1 <= runs <= 10,
             "config.determinism_runs must be an integer from 1 to 10")

    limits_raw = raw.get("limits", {})
    _check_keys(limits_raw, set(DEFAULT_LIMITS), "config.limits")
    limits = dict(DEFAULT_LIMITS)
    for key, value in limits_raw.items():
        _require(isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= LIMIT_CEILINGS[key],
                 f"config.limits.{key} must be an integer from 1 to {LIMIT_CEILINGS[key]}")
        limits[key] = value

    cmp_raw = raw.get("compare", {})
    _check_keys(cmp_raw, COMPARE_KEYS, "config.compare")
    fields = cmp_raw.get("error_fields", ["type"])
    _require(isinstance(fields, list) and "type" in fields and all(isinstance(f, str) for f in fields),
             "config.compare.error_fields must be an array of strings that includes 'type'")
    int_float = cmp_raw.get("int_float_equal", False)
    _require(isinstance(int_float, bool), "config.compare.int_float_equal must be true or false")
    compare = CompareOptions(
        error_fields=tuple(dict.fromkeys(fields)),
        float_abs_tol=_number(cmp_raw, "float_abs_tol", 0, 0, 1e300, "config.compare"),
        float_rel_tol=_number(cmp_raw, "float_rel_tol", 0, 0, 1, "config.compare"),
        int_float_equal=int_float,
    )

    controls_raw = raw.get("negative_controls", [])
    _require(isinstance(controls_raw, list), "config.negative_controls must be an array")
    controls = []
    names = set()
    for i, item in enumerate(controls_raw):
        where = f"negative_controls[{i}]"
        _check_keys(item, CONTROL_KEYS, where, required=("name", "cmd"))
        _require(isinstance(item["name"], str) and item["name"] and item["name"] not in names,
                 f"{where}.name must be a unique non-empty string")
        names.add(item["name"])
        side = _side({k: v for k, v in item.items() if k != "name"}, where, base, shared_env, SIDE_KEYS)
        side.name = f"control:{item['name']}"
        controls.append(side)

    accepted_raw = raw.get("accepted_differences", [])
    _require(isinstance(accepted_raw, list), "config.accepted_differences must be an array")
    accepted: dict[str, Accepted] = {}
    for i, item in enumerate(accepted_raw):
        where = f"accepted_differences[{i}]"
        _check_keys(item, ACCEPTED_KEYS, where, required=("case", "reason", "approved_by"))
        for key in ("case", "reason", "approved_by"):
            _require(isinstance(item[key], str) and item[key].strip(), f"{where}.{key} must be a non-empty string")
        _require(item["case"] not in accepted, f"{where}: duplicate case {item['case']!r}")
        expected = item.get("replacement")
        if expected is not None:
            _require(_valid_behavior(expected), f"{where}.replacement must look like "
                     '{"status": "ok", "output": ...} or {"status": "error", "error": {"type": ...}}')
        accepted[item["case"]] = Accepted(item["case"], item["reason"], item["approved_by"], expected)

    description = raw.get("description", "")
    _require(isinstance(description, str), "config.description must be a string")

    env_report = {
        "determinism_defaults": dict(DETERMINISM_ENV),
        "passthrough": sorted(set(BASE_PASSTHROUGH) | set(passthrough)),
        "config_env_keys": sorted(shared_overrides),
        "effective": {k: shared_env[k] for k in DETERMINISM_ENV if k in shared_env},
    }
    return Config(
        path=cfg_path, sha256=file_sha256(cfg_path), description=description, original=original,
        replacement=replacement, case_files=case_files, timeout=timeout, determinism_runs=runs,
        limits=limits, compare=compare, controls=controls, accepted=accepted, env_report=env_report,
    )


def _valid_behavior(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("status") == "ok":
        return set(obj) == {"status", "output"}
    if obj.get("status") == "error":
        return set(obj) == {"status", "error"} and isinstance(obj["error"], dict) and isinstance(
            obj["error"].get("type"), str)
    return False


def load_cases(files: list[Path], limits: dict[str, int], display_base: Path) -> tuple[list[Case], list[dict]]:
    cases: list[Case] = []
    seen: dict[str, str] = {}
    file_info = []
    for path in files:
        label = _display_path(path, display_base)
        _require(path.is_file(), f"case file not found: {label}")
        count = 0
        try:
            handle = open(path, "rb")
        except OSError as exc:
            raise UsageError(f"cannot open case file {label}: {exc}") from None
        with handle:
            for lineno, raw_line in enumerate(handle, 1):
                where = f"{label}:{lineno}"
                _require(len(raw_line) <= limits["max_case_bytes"] + 2,
                         f"{where}: case larger than limits.max_case_bytes")
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    raise UsageError(f"{where}: not valid UTF-8") from None
                if not line:
                    continue
                try:
                    obj = strict_loads(line, limits["max_depth"])
                except ValueError as exc:
                    raise UsageError(f"{where}: invalid JSON: {exc}") from None
                _check_keys(obj, CASE_KEYS, where, required=("input",))
                case_id = obj.get("id") or input_hash(obj["input"])
                _require(isinstance(case_id, str) and 0 < len(case_id) <= 200,
                         f"{where}: id must be a string of 1-200 characters")
                _require(not any(unicodedata.category(ch) in ("Cc", "Cf") for ch in case_id),
                         f"{where}: id contains control or formatting characters")
                _require(case_id not in seen, f"{where}: duplicate case id {case_id!r} (first at {seen.get(case_id)})")
                tags = obj.get("tags", [])
                _require(isinstance(tags, list) and all(isinstance(t, str) for t in tags),
                         f"{where}: tags must be an array of strings")
                seen[case_id] = where
                cases.append(Case(case_id, obj["input"], where, tags))
                count += 1
                _require(len(cases) <= limits["max_cases"], f"more than limits.max_cases ({limits['max_cases']}) cases")
        file_info.append({"path": label, "sha256": file_sha256(path), "cases": count})
    _require(cases, "no cases found; add at least one case")
    return cases, file_info


def _display_path(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Process execution


class StartError(Exception):
    """A side's command could not be started."""


def resolve_executable(side: Side) -> str | None:
    """Resolve cmd[0]: paths are relative to the side's cwd; bare names are looked up on its PATH."""
    first = side.cmd[0]
    if os.sep in first or (os.altsep and os.altsep in first):
        candidate = Path(first) if os.path.isabs(first) else side.cwd / first
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return shutil.which(first, path=side.env.get("PATH", ""))


@dataclass
class Observation:
    status: str
    output: Any = None
    error: dict[str, Any] | None = None
    detail: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    variants: list[dict[str, Any]] | None = None

    @property
    def is_behavior(self) -> bool:
        return self.status in BEHAVIOR_STATUSES

    def behavior(self) -> dict[str, Any]:
        if self.status == "ok":
            return {"status": "ok", "output": self.output}
        if self.status == "error":
            return {"status": "error", "error": self.error}
        return {"status": self.status}

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"status": self.status, "duration_ms": round(self.duration_ms, 3)}
        if self.status == "ok":
            data["output"] = self.output
        elif self.status == "error":
            data["error"] = self.error
        if self.detail:
            data["detail"] = self.detail
        if self.stderr:
            data["stderr_tail"] = self.stderr
        if self.variants:
            data["variants"] = self.variants
        return data


_EOF = object()


class _LineReader(threading.Thread):
    """Reads newline-delimited stdout into a queue, enforcing a per-line byte cap."""

    def __init__(self, stream, cap: int):
        super().__init__(daemon=True)
        self.stream, self.cap, self.lines = stream, cap, queue.Queue()

    def run(self) -> None:
        pending = bytearray()
        try:
            while True:
                chunk = self.stream.read1(1 << 16)
                if not chunk:
                    break
                pending.extend(chunk)
                while True:
                    idx = pending.find(b"\n")
                    if idx < 0:
                        break
                    line = bytes(pending[:idx])
                    del pending[: idx + 1]
                    self.lines.put(line if len(line) <= self.cap else OverflowError())
                if len(pending) > self.cap:
                    self.lines.put(OverflowError())
                    return
        except (OSError, ValueError):
            pass
        if pending:
            self.lines.put(bytes(pending))
        self.lines.put(_EOF)


class _TailReader(threading.Thread):
    """Drains stderr, keeping only the last `cap` bytes."""

    def __init__(self, stream, cap: int):
        super().__init__(daemon=True)
        self.stream, self.cap, self.buf, self.lock = stream, cap, bytearray(), threading.Lock()

    def run(self) -> None:
        try:
            for chunk in iter(lambda: self.stream.read1(1 << 16), b""):
                with self.lock:
                    self.buf.extend(chunk)
                    if len(self.buf) > self.cap:
                        del self.buf[: len(self.buf) - self.cap]
        except (OSError, ValueError):
            pass

    def take(self) -> str:
        with self.lock:
            data = bytes(self.buf)
            self.buf.clear()
        return data.decode("utf-8", "replace")


class Worker:
    """Runs one side. `process` mode starts a fresh process per request; `stream`
    mode keeps one process and restarts it after any failure."""

    def __init__(self, side: Side, timeout: float, limits: dict[str, int]):
        self.side, self.timeout, self.limits = side, timeout, limits
        self.proc: subprocess.Popen | None = None
        self.out: _LineReader | None = None
        self.err: _TailReader | None = None
        self.counter = 0

    def _start(self) -> None:
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:  # best effort; Windows is not a supported platform
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if not self.side.cwd.is_dir():
            raise StartError(f"{self.side.name}: working directory does not exist: {self.side.cwd}")
        exe = resolve_executable(self.side)
        if exe is None:
            raise StartError(f"{self.side.name}: command not found or not executable: {self.side.cmd[0]!r}")
        try:
            self.proc = subprocess.Popen(
                [exe, *self.side.cmd[1:]], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.side.cwd, env=self.side.env,
                shell=False, close_fds=True, **kwargs,
            )
        except OSError as exc:
            raise StartError(f"{self.side.name}: cannot start {self.side.cmd[0]!r}: {exc}") from None
        self.out = _LineReader(self.proc.stdout, self.limits["max_output_bytes"])
        self.err = _TailReader(self.proc.stderr, self.limits["max_stderr_bytes"])
        self.out.start()
        self.err.start()

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            proc.kill()
        except OSError:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for reader in (self.out, self.err):
            if reader:
                reader.join(timeout=1)

    def _send(self, data: bytes, close_stdin: bool) -> None:
        proc = self.proc

        def write() -> None:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
                if close_stdin:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass

        threading.Thread(target=write, daemon=True).start()

    def run(self, case_id: str, value: Any) -> Observation:
        self.counter += 1
        request_id = f"{case_id}#{self.counter}"
        payload = (wire({"id": request_id, "input": value}) + "\n").encode("ascii")
        started = time.monotonic()
        deadline = started + self.timeout
        if self.side.mode == "process" or self.proc is None or self.proc.poll() is not None:
            self.close()
            self._start()
        self._send(payload, close_stdin=self.side.mode == "process")
        obs = self._receive(request_id, deadline)
        obs.duration_ms = (time.monotonic() - started) * 1000
        if self.side.mode == "process" and obs.is_behavior:
            problem = self._finish_process(deadline)
            if problem:
                obs = Observation(problem[0], detail=problem[1], duration_ms=obs.duration_ms)
        if self.side.mode == "process" or not obs.is_behavior:
            self.close()  # also joins the stderr reader, so the tail below is complete
        if self.err is not None:
            tail = self.err.take()
            if not obs.is_behavior:
                obs.stderr = safe_text(tail[-4000:])
        return obs

    def _finish_process(self, deadline: float) -> tuple[str, str] | None:
        """After a response in process mode, require EOF and a zero exit code."""
        assert self.proc is not None and self.out is not None
        try:
            item = self.out.lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            return "timeout", "process did not exit after responding"
        if item is not _EOF:
            return "protocol_error", "extra output after the response (log to stderr, not stdout)"
        try:
            code = self.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return "timeout", "process did not exit after responding"
        return None if code == 0 else ("crash", f"exit code {code} after responding")

    def _receive(self, request_id: str, deadline: float) -> Observation:
        assert self.out is not None and self.proc is not None
        try:
            item = self.out.lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            return Observation("timeout", detail=f"no response within {self.timeout:g}s")
        if item is _EOF:
            try:
                code = self.proc.wait(timeout=max(0.05, min(1.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                code = None
            return Observation("crash", detail=f"closed stdout without a response (exit code {code})")
        if isinstance(item, OverflowError):
            return Observation("output_limit", detail=f"response larger than {self.limits['max_output_bytes']} bytes")
        return parse_response(item, request_id, self.limits["max_depth"])


def parse_response(line: bytes, request_id: str, max_depth: int) -> Observation:
    def bad(msg: str) -> Observation:
        return Observation("protocol_error", detail=msg)

    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return bad("response is not valid UTF-8")
    try:
        obj = strict_loads(text, max_depth)
    except ValueError as exc:
        return bad(f"response is not valid JSON: {exc}; first bytes: {safe_text(text[:120])!r}")
    if not isinstance(obj, dict):
        return bad("response must be a JSON object")
    unknown = sorted(set(obj) - {"id", "ok", "output", "error"})
    if unknown:
        return bad(f"unknown response key(s) {unknown}")
    if obj.get("id") != request_id:
        return bad("response id does not match request id")
    if obj.get("ok") is True:
        if "output" not in obj or "error" in obj:
            return bad("ok response must have 'output' and no 'error'")
        return Observation("ok", output=obj["output"])
    if obj.get("ok") is False:
        err = obj.get("error")
        if "output" in obj or not isinstance(err, dict) or not isinstance(err.get("type"), str) or not err["type"]:
            return bad("error response must have 'error' with a non-empty string 'type' and no 'output'")
        return Observation("error", error=err)
    return bad("response 'ok' must be true or false")


# ---------------------------------------------------------------------------
# Comparison


def compare_values(a: Any, b: Any, opts: CompareOptions, path: str = "$") -> str | None:
    """Return a description of the first difference, or None if equal."""
    if isinstance(a, bool) or isinstance(b, bool):
        if type(a) is type(b) and a == b:
            return None
        return f"{path}: {type_name(a)} {_short(a)} != {type_name(b)} {_short(b)}"
    num = (int, float)
    if isinstance(a, num) and isinstance(b, num):
        if type(a) is not type(b) and not opts.int_float_equal:
            return f"{path}: number type differs ({type_name(a)} vs {type_name(b)})"
        if isinstance(a, int) and isinstance(b, int):
            return None if a == b else f"{path}: {_short(a)} != {_short(b)}"
        fa, fb = float(a), float(b)
        tol = max(opts.float_abs_tol, opts.float_rel_tol * max(abs(fa), abs(fb)))
        return None if abs(fa - fb) <= tol else f"{path}: {_short(a)} != {_short(b)}"
    if type(a) is not type(b):
        return f"{path}: {type_name(a)} != {type_name(b)}"
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: array length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            diff = compare_values(x, y, opts, f"{path}[{i}]")
            if diff:
                return diff
        return None
    if isinstance(a, dict):
        if a.keys() != b.keys():
            only_a = sorted(set(a) - set(b))[:5]
            only_b = sorted(set(b) - set(a))[:5]
            return f"{path}: keys differ (only original: {only_a}, only replacement: {only_b})"
        for key in sorted(a):
            diff = compare_values(a[key], b[key], opts, f"{path}.{key}")
            if diff:
                return diff
        return None
    return None if a == b else f"{path}: {_short(a)} != {_short(b)}"


def type_name(v: Any) -> str:
    return {bool: "boolean", int: "integer", float: "float", str: "string", list: "array",
            dict: "object", type(None): "null"}.get(type(v), type(v).__name__)


def _short(v: Any) -> str:
    text = canonical(v)
    return text if len(text) <= 60 else text[:57] + "..."


def compare_behaviors(orig: Observation, repl: Observation, opts: CompareOptions) -> str | None:
    if orig.status != repl.status:
        return f"original returned {orig.status}, replacement returned {repl.status}"
    if orig.status == "ok":
        return compare_values(orig.output, repl.output, opts)
    for name in opts.error_fields:
        a, b = (orig.error or {}).get(name), (repl.error or {}).get(name)
        diff = compare_values(a, b, opts, f"error.{name}")
        if diff:
            return diff
    return None


def filtered_behavior(obs: Observation, opts: CompareOptions) -> dict[str, Any]:
    if obs.status == "error":
        return {"status": "error", "error": {k: obs.error[k] for k in opts.error_fields if k in obs.error}}
    return obs.behavior()


# ---------------------------------------------------------------------------
# Shrinking


def json_size(v: Any) -> tuple[int, float]:
    magnitude = 0.0
    stack = [v]
    while stack:
        item = stack.pop()
        if isinstance(item, bool):
            magnitude += item
        elif isinstance(item, (int, float)):
            magnitude += min(abs(item), 1e300)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
    return len(canonical(v)), magnitude


def shrink_candidates(v: Any) -> Iterator[Any]:
    if isinstance(v, bool):
        if v:
            yield False
    elif isinstance(v, int):
        if v != 0:
            yield 0
            if v < 0:
                yield -v
            half = int(v / 2)
            if half not in (0, v):
                yield half
            yield v - 1 if v > 0 else v + 1
    elif isinstance(v, float):
        if v != 0.0:
            yield 0.0
            if v != int(v) and abs(v) < 1e15:
                yield float(int(v))
            if v < 0:
                yield -v
    elif isinstance(v, str):
        if v:
            yield ""
            n = len(v)
            if n > 1:
                yield v[: n // 2]
                yield v[n // 2:]
            for i in range(min(n, 32)):
                yield v[:i] + v[i + 1:]
            for i, ch in enumerate(v[:32]):
                if ch != "a" and not ch.isspace():
                    yield v[:i] + "a" + v[i + 1:]
    elif isinstance(v, list):
        if v:
            yield []
            n = len(v)
            if n > 1:
                yield v[: n // 2]
                yield v[n // 2:]
            for i in range(min(n, 64)):
                yield v[:i] + v[i + 1:]
            for i in range(min(n, 64)):
                for c in shrink_candidates(v[i]):
                    yield v[:i] + [c] + v[i + 1:]
    elif isinstance(v, dict):
        keys = sorted(v)
        for k in keys:
            yield {key: v[key] for key in keys if key != k}
        for k in keys:
            for c in shrink_candidates(v[k]):
                yield {**v, k: c}


def shrink(value: Any, still_fails: Callable[[Any], bool], budget: int,
           out_of_time: Callable[[], bool]) -> tuple[Any, int]:
    """Greedy shrink. Accepts a candidate only if it is strictly smaller and still fails."""
    current, evaluations = value, 0
    improved = True
    while improved and evaluations < budget and not out_of_time():
        improved = False
        size = json_size(current)
        for cand in shrink_candidates(current):
            if json_size(cand) >= size:
                continue
            evaluations += 1
            if still_fails(cand):
                current, improved = cand, True
                break
            if evaluations >= budget or out_of_time():
                break
    return current, evaluations


# ---------------------------------------------------------------------------
# Output safety

_BIDI = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C)}


def safe_text(text: str) -> str:
    """Escape control and bidirectional-override characters so untrusted text cannot
    alter terminals or hide content in rendered Markdown."""
    out = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
        elif ch in _BIDI or unicodedata.category(ch) in ("Cc", "Cs"):
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def md_block(text: str, limit: int = 2000, lang: str = "") -> str:
    text = safe_text(text)
    if len(text) > limit:
        text = text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def md_inline(text: str, limit: int = 120) -> str:
    text = safe_text(text).replace("\n", " ").replace("\t", " ")
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") or not text else ""
    return f"{fence}{pad}{text}{pad}{fence}"


class OutputDir:
    """A private output directory that refuses to follow symlinks or clobber unrelated files."""

    def __init__(self, path: str | Path, force: bool):
        self.path = Path(path).absolute()
        if self.path.is_symlink():
            raise UsageError(f"output path is a symlink: {self.path}")
        if self.path.exists():
            _require(self.path.is_dir(), f"output path exists and is not a directory: {self.path}")
            entries = list(self.path.iterdir())
            if entries:
                _require(force, f"output directory is not empty: {self.path} (use --force to replace a previous "
                                "portparity run)")
                _require((self.path / MARKER_FILE).is_file() and not (self.path / MARKER_FILE).is_symlink(),
                         f"refusing --force: {self.path} was not created by portparity")
        else:
            self.path.mkdir(mode=0o700, parents=True)
        try:
            os.chmod(self.path, 0o700)
        except OSError:
            pass
        self.write(MARKER_FILE, f"portparity {VERSION}\n")

    def write(self, name: str, text: str) -> Path:
        target = self.path / name
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=self.path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return target


def write_new_file(path: str | Path, text: str, force: bool) -> None:
    target = Path(path).absolute()
    _require(not target.is_symlink(), f"refusing to write through symlink: {target}")
    _require(force or not target.exists(), f"file exists: {target} (use --force to overwrite)")
    _require(target.parent.is_dir(), f"directory does not exist: {target.parent}")
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Run


@dataclass
class CaseResult:
    case: Case
    verdict: str  # match | mismatch | accepted | inconclusive | not_run
    original: Observation | None = None
    replacement: Observation | None = None
    diff: str = ""
    shrunk: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"case": self.case.id, "source": self.case.source, "verdict": self.verdict}
        if self.diff:
            data["diff"] = self.diff
        data["input"] = self.case.input
        if self.original:
            data["original"] = self.original.to_json()
        if self.replacement:
            data["replacement"] = self.replacement.to_json()
        if self.shrunk:
            data["shrunk"] = self.shrunk
        return data


class Runner:
    def __init__(self, cfg: Config, deadline: float | None):
        self.cfg = cfg
        self.deadline = deadline
        self.workers: list[Worker] = []

    def worker(self, side: Side) -> Worker:
        w = Worker(side, self.cfg.timeout, self.cfg.limits)
        self.workers.append(w)
        return w

    def close(self) -> None:
        for w in self.workers:
            w.close()

    def out_of_time(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def observe(self, worker: Worker, case: Case, runs: int) -> Observation:
        first = worker.run(case.id, case.input)
        if not first.is_behavior or runs <= 1:
            return first
        for _ in range(runs - 1):
            again = worker.run(case.id, case.input)
            if not again.is_behavior or compare_behaviors(first, again, self.cfg.compare) is not None:
                variants = [first.behavior(), again.behavior() if again.is_behavior else again.to_json()]
                return Observation("unstable", detail="results differ between identical runs",
                                   variants=variants, duration_ms=first.duration_ms)
        return first


def signature(orig: Observation, repl: Observation, diff: str) -> tuple[str, ...]:
    """Identify a kind of failure: statuses, error types, and the JSON path that differs.
    Shrinking must preserve it so it cannot drift to an unrelated failure."""
    def part(obs: Observation) -> str:
        return f"error:{obs.error.get('type')}" if obs.status == "error" and obs.error else obs.status
    path = diff.split(":", 1)[0] if orig.status == repl.status == "ok" else ""
    return part(orig), part(repl), path


def classify(orig: Observation, repl: Observation, opts: CompareOptions) -> tuple[str, str]:
    if not orig.is_behavior:
        return "inconclusive", f"original {orig.status}: {orig.detail}"
    if not repl.is_behavior:
        return "mismatch", f"replacement {repl.status}: {repl.detail}"
    diff = compare_behaviors(orig, repl, opts)
    return ("mismatch", diff) if diff else ("match", "")


def run_command(args: argparse.Namespace) -> int:
    started_wall = datetime.now(timezone.utc)
    started = time.monotonic()
    cfg = load_config(args.config)
    cases, file_info = load_cases(cfg.case_files, cfg.limits, cfg.path.parent)
    if args.case:
        wanted = set(args.case)
        missing = sorted(wanted - {c.id for c in cases})
        _require(not missing, f"unknown case id(s): {missing}")
        cases = [c for c in cases if c.id in wanted]
    if args.max_cases:
        cases = cases[: args.max_cases]
    out = OutputDir(args.out, args.force)
    deadline = started + args.max_seconds if args.max_seconds else None
    runner = Runner(cfg, deadline)
    opts = cfg.compare
    results: list[CaseResult] = []
    controls: list[dict[str, Any]] = []
    interrupted = False
    start_error = ""
    progress = Progress(len(cases), quiet=args.quiet)
    shrunk_by_signature: dict[tuple[str, ...], str] = {}

    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        orig_w, repl_w = runner.worker(cfg.original), runner.worker(cfg.replacement)
        for case in cases:
            if runner.out_of_time():
                results.append(CaseResult(case, "not_run", diff="time budget exhausted"))
                continue
            orig = runner.observe(orig_w, case, cfg.determinism_runs)
            repl = runner.observe(repl_w, case, cfg.determinism_runs) if orig.is_behavior else None
            if repl is None:
                verdict, diff = "inconclusive", f"original {orig.status}: {orig.detail}"
            else:
                verdict, diff = classify(orig, repl, opts)
            result = CaseResult(case, verdict, orig, repl, diff)
            if verdict == "mismatch":
                acc = cfg.accepted.get(case.id)
                if acc and repl.is_behavior and (acc.replacement is None or compare_values(
                        acc.replacement, filtered_behavior(repl, opts), opts) is None):
                    result.verdict = "accepted"
                elif args.shrink_budget > 0:
                    sig = signature(orig, repl, diff)
                    if sig in shrunk_by_signature:
                        result.shrunk = {"reduced": False, "same_failure_as": shrunk_by_signature[sig]}
                    else:
                        shrunk_by_signature[sig] = case.id
                        result.shrunk = shrink_case(runner, orig_w, repl_w, case, orig, repl, opts,
                                                    args.shrink_budget)
            results.append(result)
            progress.step(result.verdict)
            if args.fail_fast and result.verdict == "mismatch":
                break
        done = {r.case.id for r in results}
        results.extend(CaseResult(c, "not_run", diff="stopped after first mismatch (--fail-fast)")
                       for c in cases if c.id not in done)
        oracle = {r.case.id: r.original for r in results if r.original and r.original.is_behavior}
        if cfg.controls and not args.skip_controls:
            for side in cfg.controls:
                controls.append(run_control(runner, side, cases, oracle, opts))
    except StartError as exc:
        start_error = str(exc)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        runner.close()
        signal.signal(signal.SIGTERM, previous)
        progress.finish()

    if start_error:
        print(f"portparity: {start_error}", file=sys.stderr)
        print("portparity: fix the command or run 'portparity.py validate' first. No report written.",
              file=sys.stderr)
        return EXIT_USAGE
    done = {r.case.id for r in results}
    results.extend(CaseResult(c, "not_run", diff="interrupted") for c in cases if c.id not in done)

    report = build_report(cfg, args, file_info, results, controls, started_wall, started, interrupted)
    write_reports(out, report, results)
    print_summary(report, out.path, quiet=args.quiet)
    return report["exit_code"]


def _sigterm(signum, frame):  # pragma: no cover - exercised manually
    raise KeyboardInterrupt


def shrink_case(runner: Runner, orig_w: Worker, repl_w: Worker, case: Case, orig: Observation,
                repl: Observation, opts: CompareOptions, budget: int) -> dict[str, Any] | None:
    kind = signature(orig, repl, classify(orig, repl, opts)[1])

    def still_fails(value: Any) -> bool:
        o = orig_w.run(case.id, value)
        if not o.is_behavior:
            return False
        r = repl_w.run(case.id, value)
        verdict, diff = classify(o, r, opts)
        return verdict == "mismatch" and signature(o, r, diff) == kind

    smaller, evaluations = shrink(case.input, still_fails, budget, runner.out_of_time)
    if smaller == case.input:
        return {"evaluations": evaluations, "reduced": False}
    o = orig_w.run(case.id, smaller)
    r = repl_w.run(case.id, smaller)
    verdict, diff = classify(o, r, opts) if o.is_behavior else ("inconclusive", "")
    if verdict != "mismatch":
        return {"evaluations": evaluations, "reduced": False,
                "note": "shrunk input did not reproduce on re-check; kept the original input"}
    return {"id": input_hash(smaller), "input": smaller, "evaluations": evaluations, "reduced": True,
            "diff": diff, "original": o.to_json(), "replacement": r.to_json()}


def run_control(runner: Runner, side: Side, cases: list[Case], oracle: dict[str, Observation],
                opts: CompareOptions) -> dict[str, Any]:
    worker = runner.worker(side)
    checked = 0
    for case in cases:
        if case.id not in oracle or runner.out_of_time():
            continue
        checked += 1
        obs = worker.run(case.id, case.input)
        verdict, diff = classify(oracle[case.id], obs, opts)
        if verdict == "mismatch":
            worker.close()
            return {"name": side.name.removeprefix("control:"), "rejected": True, "case": case.id,
                    "diff": diff, "cases_checked": checked}
    worker.close()
    return {"name": side.name.removeprefix("control:"), "rejected": False, "cases_checked": checked,
            "note": "the cases did not distinguish this known-wrong implementation from the original"}


def build_report(cfg: Config, args: argparse.Namespace, file_info: list[dict], results: list[CaseResult],
                 controls: list[dict], started_wall: datetime, started: float, interrupted: bool) -> dict:
    counts = {k: 0 for k in ("match", "mismatch", "accepted", "inconclusive", "not_run")}
    for r in results:
        counts[r.verdict] += 1
    seen_ids = {r.case.id: r.verdict for r in results}
    # Only cases that actually ran can show that an acceptance no longer applies.
    stale = [{"case": cid, "now": seen_ids[cid]} for cid in sorted(cfg.accepted)
             if seen_ids.get(cid, "not_run") not in ("accepted", "not_run")]
    ineffective = [c for c in controls if not c["rejected"]]

    if counts["mismatch"]:
        result, code = "fail", EXIT_MISMATCH
    elif ineffective:
        result, code = "ineffective", EXIT_INEFFECTIVE
    elif counts["inconclusive"] or counts["not_run"] or interrupted:
        result, code = "inconclusive", EXIT_INCONCLUSIVE
    else:
        result, code = "pass", EXIT_PASS
    if interrupted:
        code = EXIT_INTERRUPTED

    warnings = []
    if not cfg.controls:
        warnings.append("No negative_controls configured: there is no evidence that these cases can detect a "
                        "wrong replacement.")
    if cfg.determinism_runs < 2:
        warnings.append("determinism_runs is 1: nondeterministic results may be reported as mismatches.")
    if args.skip_controls and cfg.controls:
        warnings.append("Negative controls were skipped (--skip-controls).")
    if args.case or args.max_cases:
        warnings.append("Only a subset of cases was run (--case/--max-cases).")
    if any(s.mode == "stream" for s in (cfg.original, cfg.replacement)):
        warnings.append("Stream mode reuses one process across cases; state that leaks between cases can hide or "
                        "cause differences.")
    if stale:
        warnings.append(f"{len(stale)} accepted difference(s) no longer apply; review and remove them.")

    return {
        "tool": {"name": "portparity", "version": VERSION},
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "result": result,
        "exit_code": code,
        "interrupted": interrupted,
        "started_at": started_wall.isoformat(timespec="seconds"),
        "duration_seconds": round(time.monotonic() - started, 3),
        "platform": {"python": platform.python_version(), "system": platform.system(),
                     "release": platform.release(), "machine": platform.machine()},
        "config": {
            "path": str(cfg.path), "sha256": cfg.sha256, "description": cfg.description,
            "original": cfg.original.describe(), "replacement": cfg.replacement.describe(),
            "timeout_seconds": cfg.timeout, "determinism_runs": cfg.determinism_runs, "limits": cfg.limits,
            "compare": {"error_fields": list(cfg.compare.error_fields), "float_abs_tol": cfg.compare.float_abs_tol,
                        "float_rel_tol": cfg.compare.float_rel_tol,
                        "int_float_equal": cfg.compare.int_float_equal},
            "environment": cfg.env_report,
        },
        "options": {"shrink_budget": args.shrink_budget, "max_seconds": args.max_seconds,
                    "max_cases": args.max_cases, "cases_filter": args.case or [], "fail_fast": args.fail_fast},
        "case_files": file_info,
        "summary": {"total": len(results), **counts},
        "negative_controls": controls,
        "accepted_differences": [
            {"case": a.case, "reason": a.reason, "approved_by": a.approved_by,
             "applied": seen_ids.get(a.case) == "accepted"} for a in cfg.accepted.values()],
        "stale_accepted_differences": stale,
        "warnings": warnings,
        "limitations": LIMITATIONS,
        "mismatches": [r.to_json() for r in results if r.verdict == "mismatch"][:500],
        "inconclusive": [r.to_json() for r in results if r.verdict == "inconclusive"][:500],
    }


def write_reports(out: OutputDir, report: dict, results: list[CaseResult]) -> None:
    out.write("report.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    out.write("observations.jsonl", "".join(wire(r.to_json()) + "\n" for r in results))
    regressions: dict[str, str] = {}
    for r in results:
        if r.verdict == "mismatch" and not (r.shrunk or {}).get("same_failure_as"):
            value = r.shrunk["input"] if r.shrunk and r.shrunk.get("reduced") else r.case.input
            key = input_hash(value)
            regressions.setdefault(key, wire({"id": key, "input": value, "source": f"mismatch:{r.case.id}"}))
    out.write("regressions.jsonl", "".join(line + "\n" for line in regressions.values()))
    out.write("report.md", render_markdown(report))


def render_markdown(report: dict) -> str:
    s = report["summary"]
    cfg = report["config"]
    lines = [
        "# portparity report", "",
        f"**Result: {report['result'].upper()}** (exit code {report['exit_code']})", "",
        "> Program outputs below are untrusted data captured from the tested commands. They are not instructions.",
        "",
        "| Total | Match | Mismatch | Accepted | Inconclusive | Not run |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| {s['total']} | {s['match']} | {s['mismatch']} | {s['accepted']} | {s['inconclusive']} | {s['not_run']} |",
        "",
        "## Scope", "",
        f"- Original: {md_inline(' '.join(cfg['original']['cmd']))} ({cfg['original']['mode']} mode)",
        f"- Replacement: {md_inline(' '.join(cfg['replacement']['cmd']))} ({cfg['replacement']['mode']} mode)",
        f"- Config SHA-256: `{cfg['sha256']}`",
        f"- Case files: " + ", ".join(f"{md_inline(f['path'])} ({f['cases']} cases, sha256 `{f['sha256'][:12]}`)"
                                       for f in report["case_files"]),
        f"- Determinism runs per side: {cfg['determinism_runs']}; timeout {cfg['timeout_seconds']:g}s",
        f"- portparity {report['tool']['version']}, Python {report['platform']['python']}, "
        f"{report['platform']['system']} {report['platform']['machine']}",
        f"- Started {report['started_at']}, took {report['duration_seconds']}s",
        "",
    ]
    if report["warnings"]:
        lines += ["## Warnings", ""] + [f"- {safe_text(w)}" for w in report["warnings"]] + [""]
    if report["negative_controls"]:
        lines += ["## Negative controls", "",
                  "Each known-wrong implementation must be rejected by at least one case.", ""]
        for c in report["negative_controls"]:
            state = "rejected" if c["rejected"] else "**NOT rejected**"
            where = f" by case {md_inline(c['case'])}" if c.get("case") else ""
            lines.append(f"- {md_inline(c['name'])}: {state}{where} after {c['cases_checked']} case(s)")
        lines.append("")
    if report["mismatches"]:
        lines += ["## Mismatches", ""]
        for m in report["mismatches"][:50]:
            lines += [f"### Case {md_inline(m['case'])}", "", f"Source: {md_inline(m['source'])}", "",
                      "Difference:", md_block(m.get("diff", "")), "", "Input:", md_block(canonical(m["input"])),
                      "", "Original:", md_block(canonical(m["original"])), "", "Replacement:",
                      md_block(canonical(m["replacement"])), ""]
            sh = m.get("shrunk")
            if sh and sh.get("reduced"):
                lines += [f"Smallest failing input found ({sh['evaluations']} evaluations):",
                          md_block(canonical(sh["input"])), "", "Difference on that input:",
                          md_block(sh.get("diff", "")), ""]
        if len(report["mismatches"]) > 50:
            lines += [f"... {len(report['mismatches']) - 50} more in report.json and observations.jsonl.", ""]
    if report["inconclusive"]:
        lines += ["## Inconclusive cases", "",
                  "The original did not produce a stable, valid result, so these cases test nothing.", ""]
        for m in report["inconclusive"][:50]:
            lines.append(f"- {md_inline(m['case'])}: {md_inline(m.get('diff', ''), 200)}")
        lines.append("")
    if report["accepted_differences"]:
        lines += ["## Accepted differences", ""]
        for a in report["accepted_differences"]:
            lines.append(f"- {md_inline(a['case'])}: {md_inline(a['reason'], 300)} (approved by "
                         f"{md_inline(a['approved_by'])}; {'applied' if a['applied'] else 'not applied'})")
        lines.append("")
    lines += ["## Limitations", ""] + [f"- {item}" for item in report["limitations"]] + [""]
    return "\n".join(lines)


class Progress:
    def __init__(self, total: int, quiet: bool):
        self.total, self.quiet, self.done, self.next = total, quiet or not sys.stderr.isatty(), 0, time.monotonic()
        self.counts: dict[str, int] = {}

    def step(self, verdict: str) -> None:
        self.done += 1
        self.counts[verdict] = self.counts.get(verdict, 0) + 1
        if not self.quiet and time.monotonic() >= self.next:
            self.next = time.monotonic() + 0.5
            parts = " ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
            print(f"\r[{self.done}/{self.total}] {parts}", end="", file=sys.stderr, flush=True)

    def finish(self) -> None:
        if not self.quiet and self.done:
            print(file=sys.stderr)


def print_summary(report: dict, out: Path, quiet: bool) -> None:
    s = report["summary"]
    print(f"portparity: {report['result'].upper()} - {s['match']} match, {s['mismatch']} mismatch, "
          f"{s['accepted']} accepted, {s['inconclusive']} inconclusive, {s['not_run']} not run "
          f"(of {s['total']})")
    for c in report["negative_controls"]:
        print(f"  negative control {safe_text(c['name'])!r}: {'rejected' if c['rejected'] else 'NOT REJECTED'}")
    if not quiet:
        for m in report["mismatches"][:5]:
            diff = safe_text(m.get("diff", ""))[:160]
            print(f"  mismatch {safe_text(m['case'])!r}: {diff}")
        for w in report["warnings"]:
            print(f"  warning: {safe_text(w)}")
    print(f"  report: {out / 'report.md'}")


# ---------------------------------------------------------------------------
# Case generation from a JSON Schema subset

SCHEMA_KEYS = {
    "type", "enum", "const", "minimum", "maximum", "minLength", "maxLength", "items", "minItems",
    "maxItems", "properties", "required", "additionalProperties", "anyOf", "oneOf",
    "title", "description", "$comment", "examples", "x-alphabet",
}
GEN_KEYS = {"seed", "count", "edge_probability", "schema", "include"}
DEFAULT_ALPHABET = (
    "abcxyzABCXYZ0129 _-.,'\"\\/@#\t\n"
    "éßİı  　€µÅÅﬁ\U0001F600\u0000\u001f\u0085"
)
INT_DEFAULT_RANGE = (-(2 ** 31), 2 ** 31 - 1)


class Generator:
    def __init__(self, seed: int, edge_probability: float):
        self.rng = random.Random(seed)
        self.edge = edge_probability

    def check(self, schema: Any, path: str = "schema") -> None:
        _require(isinstance(schema, dict), f"{path} must be an object")
        unknown = sorted(set(schema) - SCHEMA_KEYS)
        _require(not unknown, f"{path}: unsupported schema keyword(s) {unknown}. Supported: {sorted(SCHEMA_KEYS)}")
        if schema.get("additionalProperties") not in (None, False):
            raise UsageError(f"{path}.additionalProperties: only false is supported (extra keys are never generated)")
        types = schema.get("type")
        if types is not None:
            tlist = types if isinstance(types, list) else [types]
            valid = {"integer", "number", "string", "boolean", "null", "array", "object"}
            _require(tlist and all(t in valid for t in tlist), f"{path}.type must be one of {sorted(valid)}")
        _require(any(k in schema for k in ("type", "enum", "const", "anyOf", "oneOf")),
                 f"{path}: needs one of type, enum, const, anyOf, oneOf")
        if "enum" in schema:
            _require(isinstance(schema["enum"], list) and schema["enum"], f"{path}.enum must be a non-empty array")
        for key in ("anyOf", "oneOf"):
            if key in schema:
                _require(isinstance(schema[key], list) and schema[key], f"{path}.{key} must be a non-empty array")
                for i, sub in enumerate(schema[key]):
                    self.check(sub, f"{path}.{key}[{i}]")
        if "items" in schema:
            self.check(schema["items"], f"{path}.items")
        _require(isinstance(schema.get("properties", {}), dict), f"{path}.properties must be an object")
        for name, sub in schema.get("properties", {}).items():
            self.check(sub, f"{path}.properties.{name}")
        req = schema.get("required", [])
        _require(isinstance(req, list) and all(r in schema.get("properties", {}) for r in req),
                 f"{path}.required must list keys defined in properties")
        lo, hi = schema.get("minimum"), schema.get("maximum")
        for name, bound in (("minimum", lo), ("maximum", hi)):
            _require(bound is None or (isinstance(bound, (int, float)) and not isinstance(bound, bool)
                                       and math.isfinite(bound)), f"{path}.{name} must be a finite number")
        if lo is not None and hi is not None:
            _require(lo <= hi, f"{path}: minimum > maximum")
        if "x-alphabet" in schema:
            _require(isinstance(schema["x-alphabet"], str) and schema["x-alphabet"],
                     f"{path}.x-alphabet must be a non-empty string")
        for a, b in (("minLength", "maxLength"), ("minItems", "maxItems")):
            if a in schema or b in schema:
                x, y = schema.get(a, 0), schema.get(b, max(schema.get(a, 0), 8))
                _require(isinstance(x, int) and isinstance(y, int) and 0 <= x <= y <= 10_000,
                         f"{path}: invalid {a}/{b}")

    def value(self, schema: dict) -> Any:
        rng = self.rng
        if "const" in schema:
            return schema["const"]
        if "enum" in schema:
            return rng.choice(schema["enum"])
        for key in ("anyOf", "oneOf"):
            if key in schema:
                return self.value(rng.choice(schema[key]))
        types = schema["type"]
        kind = rng.choice(types) if isinstance(types, list) else types
        edge = rng.random() < self.edge
        if kind == "null":
            return None
        if kind == "boolean":
            return rng.random() < 0.5
        if kind == "integer":
            lo, hi = _bounds(schema, *INT_DEFAULT_RANGE)
            lo, hi = math.ceil(lo), math.floor(hi)
            _require(lo <= hi, "integer schema has no integer between minimum and maximum")
            if edge:
                picks = [v for v in (lo, hi, 0, 1, -1, lo + 1, hi - 1) if lo <= v <= hi]
                return rng.choice(picks)
            if rng.random() < 0.5:
                small_lo, small_hi = max(lo, -100), min(hi, 100)
                if small_lo <= small_hi:
                    return rng.randint(small_lo, small_hi)
            return rng.randint(lo, hi)
        if kind == "number":
            lo, hi = (float(b) for b in _bounds(schema, -1e6, 1e6))
            if edge:
                picks = [v for v in (lo, hi, 0.0, 0.5, -0.5, 1.5, 2.5, 0.1, 1e-9) if lo <= v <= hi]
                return rng.choice(picks)
            v = rng.uniform(lo, hi)
            if rng.random() < 0.5:
                r = round(v, rng.choice((0, 1, 2, 3, 6)))
                return r if lo <= r <= hi else v
            return v
        if kind == "string":
            lo = schema.get("minLength", 0)
            hi = schema.get("maxLength", max(lo, 12))
            alphabet = schema.get("x-alphabet", DEFAULT_ALPHABET)
            n = rng.choice((lo, hi)) if edge else rng.randint(lo, hi)
            return "".join(rng.choice(alphabet) for _ in range(n))
        if kind == "array":
            lo = schema.get("minItems", 0)
            hi = schema.get("maxItems", max(lo, 5))
            n = rng.choice((lo, hi)) if edge else rng.randint(lo, hi)
            items = schema.get("items", {"type": ["integer", "string", "null"]})
            return [self.value(items) for _ in range(n)]
        if kind == "object":
            props = schema.get("properties", {})
            required = set(schema.get("required", []))
            return {k: self.value(sub) for k, sub in props.items() if k in required or rng.random() < 0.7}
        raise UsageError(f"unsupported type {kind!r}")


def _bounds(schema: dict, default_lo: float, default_hi: float) -> tuple[float, float]:
    span = default_hi - default_lo
    lo, hi = schema.get("minimum"), schema.get("maximum")
    if lo is None and hi is None:
        return default_lo, default_hi
    if lo is None:
        return hi - span, hi
    if hi is None:
        return lo, lo + span
    return lo, hi


def generate_command(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    _require(spec_path.is_file(), f"spec not found: {args.spec}")
    try:
        spec = strict_loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise UsageError(f"cannot read spec {args.spec}: {exc}") from None
    _check_keys(spec, GEN_KEYS, "spec", required=("schema",))
    seed = args.seed if args.seed is not None else spec.get("seed", 0)
    count = args.count if args.count is not None else spec.get("count", 100)
    edge = spec.get("edge_probability", 0.25)
    _require(isinstance(seed, int) and not isinstance(seed, bool), "seed must be an integer")
    _require(isinstance(count, int) and 1 <= count <= 1_000_000, "count must be an integer from 1 to 1000000")
    _require(isinstance(edge, (int, float)) and 0 <= edge <= 1, "edge_probability must be between 0 and 1")
    include = spec.get("include", [])
    _require(isinstance(include, list), "include must be an array of input values")
    gen = Generator(seed, float(edge))
    gen.check(spec["schema"])
    seen: dict[str, None] = {}
    lines = []
    for value in include:
        key = input_hash(value)
        if key not in seen:
            seen[key] = None
            lines.append(wire({"id": key, "input": value, "source": "include"}))
    attempts = 0
    target = len(seen) + count
    while len(seen) < target and attempts < count * 20:
        attempts += 1
        value = gen.value(spec["schema"])
        key = input_hash(value)
        if key in seen:
            continue
        seen[key] = None
        lines.append(wire({"id": key, "input": value, "source": f"generated:seed={seed}"}))
    write_new_file(args.out, "".join(line + "\n" for line in lines), args.force)
    print(f"portparity: wrote {len(lines)} unique case(s) to {args.out} (seed {seed}, {attempts} attempts)")
    if len(seen) < target:
        print("portparity: warning: the schema allows fewer unique values than requested", file=sys.stderr)
    return EXIT_PASS


# ---------------------------------------------------------------------------
# Validate


def validate_command(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    cases, file_info = load_cases(cfg.case_files, cfg.limits, cfg.path.parent)
    problems = []
    for side in (cfg.original, cfg.replacement, *cfg.controls):
        if not side.cwd.is_dir():
            problems.append(f"{side.name}: working directory does not exist: {side.cwd}")
        elif resolve_executable(side) is None:
            problems.append(f"{side.name}: command not found or not executable: {side.cmd[0]!r}")
    ids = {c.id for c in cases}
    for cid in cfg.accepted:
        if cid not in ids:
            problems.append(f"accepted difference refers to unknown case {cid!r}")
    print(f"portparity: config OK ({len(cases)} cases in {len(file_info)} file(s))")
    if not cfg.controls:
        print("  warning: no negative_controls; add a known-wrong implementation to prove the cases can fail")
    for p in problems:
        print(f"  error: {safe_text(p)}", file=sys.stderr)
    return EXIT_USAGE if problems else EXIT_PASS


# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portparity",
        description="Differential behavior testing for code migrations. Exit codes: 0 pass, 1 mismatch, "
                    "2 usage error, 3 inconclusive, 4 negative control not rejected, 130 interrupted.",
    )
    parser.add_argument("--version", action="version", version=f"portparity {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run original and replacement on all cases and write a report")
    run.add_argument("config", help="portparity JSON config")
    run.add_argument("--out", required=True, help="output directory (created; must be empty or a previous run)")
    run.add_argument("--force", action="store_true", help="replace a previous portparity run in --out")
    run.add_argument("--shrink-budget", type=_nonneg_int, default=200,
                     help="max shrink evaluations per mismatch (0 disables; default 200)")
    run.add_argument("--max-seconds", type=_pos_float, default=None,
                     help="wall-clock budget; remaining cases are reported as not run")
    run.add_argument("--max-cases", type=_pos_int, default=None, help="run only the first N cases")
    run.add_argument("--case", action="append", help="run only this case id (repeatable)")
    run.add_argument("--fail-fast", action="store_true", help="stop after the first mismatch")
    run.add_argument("--skip-controls", action="store_true", help="do not run negative controls")
    run.add_argument("--quiet", action="store_true", help="print only the summary line")
    run.set_defaults(func=run_command)

    val = sub.add_parser("validate", help="check config and cases without running anything")
    val.add_argument("config")
    val.set_defaults(func=validate_command)

    gen = sub.add_parser("generate", help="generate JSONL cases from a JSON Schema subset")
    gen.add_argument("spec", help="generator spec JSON with a 'schema' key")
    gen.add_argument("--out", required=True, help="output JSONL file")
    gen.add_argument("--count", type=_pos_int, default=None)
    gen.add_argument("--seed", type=int, default=None)
    gen.add_argument("--force", action="store_true", help="overwrite an existing file")
    gen.set_defaults(func=generate_command)
    return parser


def _nonneg_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _pos_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _pos_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        print("portparity: Python 3.10 or newer is required", file=sys.stderr)
        return EXIT_USAGE
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UsageError as exc:
        print(f"portparity: error: {safe_text(str(exc))}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("portparity: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
