# Example: legacy C# shipping fee to Python

| Path | Role |
| --- | --- |
| `original/ShippingRules.cs` | The legacy rule: the oracle. It trims the zone with .NET whitespace rules, upper-cases it, and rounds with banker's rounding |
| `original/Program.cs` | Its adapter: JSON lines in and out, exceptions mapped to `bad_input`, `invalid_argument`, `unknown_zone` |
| `replacement/` | A correct Python port. It uses `Decimal` with `ROUND_HALF_EVEN` and the exact .NET whitespace set, because Python's `str.strip()` also removes U+001C to U+001F |
| `broken/adapter.mjs` | A plausible but wrong JavaScript port, used as the negative control and in the failing demo |
| `cases/fixtures.jsonl` | Hand-written cases, the kind a component owner would provide |
| `cases/generate.json` | Generator spec; `cases/generated.jsonl` is its committed output (seed 20261109) |
| `portparity.json` | Original vs. Python port, with the JavaScript port as a negative control |
| `portparity-broken.json` | Original vs. JavaScript port, to show a failing run |

```sh
dotnet build original -c Release -o original/out
python3 ../../skills/portparity/scripts/portparity.py run portparity.json --out /tmp/pp-good
python3 ../../skills/portparity/scripts/portparity.py run portparity-broken.json --out /tmp/pp-bad
```

Measured on macOS (Python 3.14, .NET 10, Node.js 24) on November 9, 2026:

- `portparity.json`: PASS, 311 of 311 cases matched, and the negative control was rejected.
- `portparity-broken.json`: FAIL, 111 mismatches shrunk to 3 regression cases, one per bug:
  `{"weight_grams":2,"zone":"A","express":false}` (rounding),
  `{"weight_grams":1,"zone":"","express":false}` (unknown zone falls back to A), and
  `{"zone":""}` (missing input validation).

The cases cover the tested inputs only. They show the workflow, not the correctness of any other
port. While building this example, the runner also exposed a transport hazard: Node's `readline`
splits lines at U+2028. Requests are now always sent as ASCII-escaped JSON.
