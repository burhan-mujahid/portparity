# Adapter protocol

portparity talks to each side through an **adapter**: a small program that reads requests on
stdin and writes responses on stdout. Templates for Python, Node.js, and .NET are in `../templates/`.

## Messages

Each request is one line of ASCII-only JSON (non-ASCII characters are `\u`-escaped):

```json
{"id": "typical-a#1", "input": {"weight_grams": 1200, "zone": "A"}}
```

The adapter writes exactly one line per request, echoing `id`:

```json
{"id": "typical-a#1", "ok": true, "output": {"fee": "3.00"}}
{"id": "typical-a#1", "ok": false, "error": {"type": "invalid_argument", "message": "weight must be positive"}}
```

| Field | Rule |
| --- | --- |
| `id` | Must equal the request `id` exactly. Treat it as an opaque string. |
| `ok` | `true` or `false`. |
| `output` | Required when `ok` is true; any JSON value, including `null`. |
| `error` | Required when `ok` is false; an object with a non-empty string `type`. Other fields are allowed. |

No other top-level keys are allowed. Responses must be UTF-8, RFC 8259 JSON with no NaN or
Infinity, no duplicate keys, no lone surrogates, and nesting no deeper than `limits.max_depth`.

By default only `error.type` is compared. Messages often differ between runtimes; add `"message"`
to `compare.error_fields` only when the message is part of the contract.

## Modes

- **`process`** (default): a new process per request. The runner writes one request, closes stdin,
  and expects one response line, then EOF and exit code 0. This is the safest choice because no
  state carries over between cases.
- **`stream`**: one long-lived process. The runner writes a request, waits for the response line,
  then sends the next. After any timeout, crash, or protocol error the process is killed and
  restarted. Use it when startup is slow (JVM, .NET, large imports). State that leaks between
  cases can hide or cause differences; `determinism_runs` (default 2) catches some of this.

The same adapter loop (`for each line in stdin: respond`) works in both modes.

## What counts as behavior

| Status | Meaning | Counted as |
| --- | --- | --- |
| `ok` | Valid response with `output` | Behavior (compared) |
| `error` | Valid response with `error` | Behavior (compared by `error_fields`) |
| `crash` | Exited, or closed stdout, without a valid response; or nonzero exit | Failure |
| `timeout` | No response within `timeout_seconds` | Failure |
| `protocol_error` | Invalid JSON, wrong `id`, unknown keys, or extra stdout output | Failure |
| `output_limit` | Response line larger than `limits.max_output_bytes` | Failure |
| `unstable` | Repeated runs of the same input gave different results | Failure |

A failure on the **original** makes the case *inconclusive*: it tested nothing. A failure on the
**replacement** is a *mismatch*. If an exception is the original's real behavior, catch it in the
adapter and report it as an `error`. Don't let the process crash.

## Process handling

- Commands are argument arrays executed **without a shell**. Relative executable paths resolve
  against the side's `cwd`. Bare names are found on the side's `PATH`.
- Each process starts in its own process group. On timeout the whole group is killed, including
  child processes.
- stdin is written from a separate thread, so an adapter that never reads stdin cannot block the runner.
- stdout lines and the stderr tail are size-capped. The last 4,000 characters of stderr are kept
  for failures only, with control characters escaped.
- The environment is rebuilt, not inherited: `PATH`, `HOME`, `TMPDIR`, `TEMP`, `TMP`, and
  `SYSTEMROOT`, plus `TZ=UTC`, `LC_ALL=C.UTF-8`, `LANG=C.UTF-8`, and `PYTHONUTF8=1`, plus the
  config's `env_passthrough` and `env`. Both sides get the same base environment.

## Cross-language encoding

| Value | Hazard | Recommendation |
| --- | --- | --- |
| Decimal or money | Binary floats change values | Strings formatted with an invariant culture: `"12.30"` |
| Integers beyond 2^53 | JavaScript and some JSON libraries lose precision | Strings |
| `1` vs `1.0` | JavaScript and some serializers cannot tell them apart | Strings, or `int_float_equal` |
| Floats | Last-bit differences between runtimes | `float_abs_tol` / `float_rel_tol`, agreed with the user |
| Dates and times | Time zone, culture, precision | ISO 8601 strings in UTC; pass "now" as input |
| Bytes | Not representable in JSON | Base64 strings |
| Maps with ordering | JSON objects are unordered | Arrays of pairs when order matters |
| Errors | Native exception names differ | A shared vocabulary in both adapters |

## Replay adapters (when the original cannot run)

If the user has recorded inputs and outputs from the original (logs, a previous test run), an
adapter can look a request's input up in that recording. It must **exit nonzero for any input not
in the recording**, so unrecorded inputs become inconclusive rather than fake behavior. Generated
cases and shrinking are of little use in this mode. Say clearly in the report that the oracle was
a recording, and give its source and date.
