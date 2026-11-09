# Configuration and CLI reference

## Config file (`portparity.json`)

Paths are relative to the config file. Unknown keys are rejected, so typos fail loudly.

```json
{
  "version": 1,
  "description": "Legacy C# shipping rule vs. Python port",
  "original":    {"cmd": ["dotnet", "original/out/Legacy.dll"], "mode": "stream", "cwd": ".", "env": {}},
  "replacement": {"cmd": ["python3", "replacement/adapter.py"], "mode": "stream"},
  "cases": ["cases/fixtures.jsonl", "cases/generated.jsonl", "cases/regressions.jsonl"],
  "timeout_seconds": 10,
  "determinism_runs": 2,
  "env": {"FEATURE_FLAG": "off"},
  "env_passthrough": ["DOTNET_ROOT"],
  "limits": {"max_case_bytes": 1048576, "max_output_bytes": 1048576, "max_stderr_bytes": 65536,
             "max_cases": 100000, "max_depth": 64},
  "compare": {"error_fields": ["type"], "float_abs_tol": 0, "float_rel_tol": 0, "int_float_equal": false},
  "negative_controls": [{"name": "half-up-rounding", "cmd": ["python3", "control/adapter.py"]}],
  "accepted_differences": [
    {"case": "midpoint-2", "reason": "Owner chose half-up rounding for the new system",
     "approved_by": "Jane Doe (product owner)",
     "replacement": {"status": "ok", "output": {"fee": "0.01"}}}
  ]
}
```

| Key | Default | Notes |
| --- | --- | --- |
| `version` | required | Must be `1`. |
| `original`, `replacement` | required | `cmd` (array of strings), optional `cwd`, `env`, `mode` (`process` or `stream`). |
| `cases` | required | Non-empty list of JSONL files. |
| `timeout_seconds` | 10 | Per request, from 0.05 to 3600. |
| `determinism_runs` | 2 | Runs per side per case (1 to 10). Differing runs are reported as `unstable`. |
| `env` | `{}` | Added to both sides. Values are not written to reports, only key names. |
| `env_passthrough` | `[]` | Names of parent variables to copy, in addition to the base set. |
| `limits` | see above | Upper bounds for case size, response size, stderr tail, case count, JSON depth. |
| `compare.error_fields` | `["type"]` | Must include `type`. |
| `compare.float_abs_tol`, `float_rel_tol` | 0 | Equal if `abs(a-b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))`. |
| `compare.int_float_equal` | false | When false, `1` and `1.0` differ. Booleans never equal numbers. |
| `negative_controls` | `[]` | Known-wrong implementations (`name` plus a side spec). Each must produce at least one mismatch. |
| `accepted_differences` | `[]` | `case`, `reason`, and `approved_by` are required. Optional `replacement` pins the exact accepted result (compared fields only). Otherwise any replacement result counts as accepted. |

## Case files (JSONL)

One JSON object per line. Blank lines are ignored.

```json
{"id": "midpoint-2", "input": {"weight_grams": 2, "zone": "A"}, "tags": ["rounding"], "note": "0.005 rounds to even"}
{"input": {"weight_grams": 7, "zone": "B"}}
```

`input` is required. `id` defaults to `h-` plus a hash of the canonical input, so it stays
stable across reorderings. IDs must be unique and contain no control or formatting characters.
Allowed keys: `id`, `input`, `tags`, `note`, `source`.

## Generator spec (`generate`)

```json
{
  "seed": 42,
  "count": 300,
  "edge_probability": 0.25,
  "include": [{"weight_grams": 2, "zone": "A"}],
  "schema": {
    "type": "object",
    "required": ["weight_grams", "zone"],
    "properties": {
      "weight_grams": {"type": "integer", "minimum": -5, "maximum": 250000},
      "zone": {"anyOf": [{"enum": ["A", "B", " a "]}, {"type": "string", "maxLength": 3}, {"type": "null"}]}
    }
  }
}
```

The supported JSON Schema subset is `type` (single or array), `enum`, `const`, `minimum`,
`maximum`, `minLength`, `maxLength`, `items`, `minItems`, `maxItems`, `properties`, `required`,
`additionalProperties: false`, `anyOf`, `oneOf`, and annotations (`title`, `description`,
`$comment`, `examples`). The extension `x-alphabet` sets the characters used for strings. Any
other keyword is an error, so constraints are never silently ignored.

With probability `edge_probability`, a value comes from the boundaries (minimum, maximum, 0, ±1,
minimum and maximum length). The default string alphabet includes non-ASCII letters, Unicode
whitespace, NUL, an emoji, and quote characters. Optional properties appear 70% of the time.
Output is de-duplicated. A given seed reproduces the same file on the same Python version.
Commit the generated file rather than relying on regeneration.

## CLI

```
portparity.py validate CONFIG
portparity.py generate SPEC --out FILE [--count N] [--seed S] [--force]
portparity.py run CONFIG --out DIR [--force] [--shrink-budget N] [--max-seconds S]
                                   [--max-cases N] [--case ID]... [--fail-fast] [--skip-controls] [--quiet]
```

- `--out` must not exist, or must be an empty directory. `--force` replaces only a directory that
  holds a previous portparity run (it checks for a `.portparity-run` marker) and refuses symlinks.
- `--shrink-budget` (default 200) is the number of evaluations spent shrinking the first mismatch
  of each failure type. A failure type is the pair of statuses, the error types, and the differing JSON path.
- `--max-seconds` is a wall-clock budget. Cases left over are `not_run` and the exit code is 3.

## Output files (mode 0600, in a 0700 directory)

| File | Contents |
| --- | --- |
| `report.json` | Result, exit code, environment, config and case-file SHA-256, summary, controls, accepted and stale differences, warnings, limitations, and up to 500 mismatches and inconclusive cases |
| `report.md` | A human-readable version. Program output is fenced and control characters are escaped |
| `observations.jsonl` | Every case with both observations (ASCII JSON) |
| `regressions.jsonl` | One minimal failing input per failure type, ready to append to a case file |
