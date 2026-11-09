# portparity

**Port legacy code with evidence.** portparity is an agent skill for Claude Code and Codex, plus a
dependency-free Python runner. It runs the original implementation and its replacement on the same
inputs, shrinks every kind of mismatch to a minimal regression case, and writes a report that
states exactly what was tested and what still differs.

```text
$ portparity.py run portparity-broken.json --out runs/1
portparity: FAIL - 200 match, 111 mismatch, 0 accepted, 0 inconclusive, 0 not run (of 311)
  mismatch 'midpoint-rounds-to-even': $.fee: "0.00" != "0.01"
  mismatch 'unknown-zone': original returned error, replacement returned ok
  mismatch 'wrong-type-weight': original returned error, replacement returned ok
  ...
$ cat runs/1/regressions.jsonl
{"id":"h-2cc22bd5d7822145","input":{"express":false,"weight_grams":2,"zone":"A"},"source":"mismatch:midpoint-rounds-to-even"}
{"id":"h-b9dd1b3257042d3d","input":{"express":false,"weight_grams":1,"zone":""},"source":"mismatch:unknown-zone"}
{"id":"h-79cb886ea8f2f093","input":{"zone":""},"source":"mismatch:wrong-type-weight"}
```

That output is from the included [example](examples/shipping-fee/): a legacy C# shipping-fee rule
checked against a plausible JavaScript port. The port's three bugs (half-up instead of banker's
rounding, a silent fallback for unknown zones, and no input validation) are reduced to three
one-line regression cases. A correct Python port of the same rule passes all 311 cases, and the
JavaScript port is kept as a negative control that the cases must reject.

## Why

Coding agents can translate code quickly. Without a reference, though, they write tests that agree
with their own translation. portparity makes the **running original the oracle**:

- **Differential, not hand-written.** Every case runs on both implementations. Expected values
  are never typed by hand or guessed by a model.
- **Language-neutral.** Each side is a command speaking JSON lines over stdin and stdout, so any
  pair works: C# to Python, Java to Kotlin, COBOL to Java, Python 2 to 3, or v1 to v2 of a library.
- **Minimal failures.** Mismatches are shrunk while preserving the failure type, so one bug
  becomes one small case rather than 100 noisy ones.
- **The harness is tested too.** Negative controls (known-wrong implementations) must be rejected,
  repeated runs detect nondeterminism, and an oracle that crashes makes a case *inconclusive*,
  never *passed*.
- **Honest output.** Reports state the scope, the case files and their hashes, and the
  limitations. Passing cases are evidence for those inputs, not a proof of equivalence.

## Install

The repository is both a Claude Code plugin marketplace and a Codex plugin marketplace. The
skill is in [`skills/portparity/`](skills/portparity/SKILL.md).

**Claude Code**

```sh
claude plugin marketplace add burhan-mujahid/portparity
claude plugin install portparity@portparity
```

**Codex**

```sh
codex plugin marketplace add burhan-mujahid/portparity
codex plugin add portparity@portparity
```

**Manual (any agent that reads `SKILL.md` folders)**

```sh
git clone https://github.com/burhan-mujahid/portparity
cp -R portparity/skills/portparity ~/.claude/skills/    # Claude Code
cp -R portparity/skills/portparity ~/.agents/skills/    # Codex
```

**Without an agent:** the runner is a single file needing Python 3.10+ and nothing else:
`python3 skills/portparity/scripts/portparity.py --help`.

## Use it

Ask your agent something like:

> Port `PricingRules.cs` to Python. Use portparity to check the port against the original.

The skill guides the agent through the workflow:

1. **Agree the contract** with you: what must be preserved, and what is intentionally changing.
2. **Wrap the original** in a small adapter ([templates](skills/portparity/templates/) for Python, Node.js, .NET).
3. **Build cases** from your examples, hand-written edge cases, and the seeded generator.
4. **Test the harness**: the original against itself must pass, and a planted bug must be caught.
5. **Write the replacement**, run, fix, and add shrunk regressions until it passes.
6. **Report** what matched, on which cases, and what was not tested.

The guardrails in [`SKILL.md`](skills/portparity/SKILL.md) forbid the usual shortcuts. The agent
may not edit the original or the cases to get a pass, may not loosen comparisons without your
agreement, and may not mark a difference as accepted unless you approved it by name.

## Try the example

Requires the .NET 10 SDK, Node.js 18+, and Python 3.10+.

```sh
cd examples/shipping-fee
dotnet build original -c Release -o original/out
python3 ../../skills/portparity/scripts/portparity.py run portparity.json --out /tmp/pp-good          # exit 0
python3 ../../skills/portparity/scripts/portparity.py run portparity-broken.json --out /tmp/pp-bad    # exit 1
```

## How it works

```text
cases/*.jsonl ──► portparity ──► original adapter    (oracle)  ─┐
                       │                                        ├─► compare ─► shrink ─► report
                       └───────► replacement adapter            ─┘
```

- **Protocol:** one ASCII JSON request per line, `{"id", "input"}`. One response per line:
  `{"id", "ok": true, "output"}` or `{"id", "ok": false, "error": {"type"}}`. See
  [protocol.md](skills/portparity/references/protocol.md).
- **Comparison:** strict JSON types (`true` is not `1`, `1` is not `1.0` unless configured),
  optional float tolerances, and errors compared by type, or more fields if configured.
- **Modes:** `process` (a fresh process per case) or `stream` (one long-lived process, restarted after any failure).
- **Outputs:** `report.json`, `report.md`, `observations.jsonl`, and `regressions.jsonl`. Exit
  codes: 0 pass, 1 mismatch, 2 usage error, 3 inconclusive, 4 harness ineffective, 130 interrupted.

Full reference: [config.md](skills/portparity/references/config.md).

## Security

portparity runs commands you configure, as your user. It is **not a sandbox**. Built-in safeguards:

- No shell: commands are argument arrays.
- A rebuilt environment, so secrets in your shell are not inherited by default.
- Per-case timeouts that kill the whole process group.
- Size limits on inputs, outputs, and stderr, and strict JSON parsing with a depth limit.
- Private (0600) report files, atomic writes, refusal to write through symlinks, and no overwriting of unrelated directories.
- Escaping of control and bidirectional-override characters in reports.

Run untrusted or side-effecting code inside a container or VM. See [SECURITY.md](SECURITY.md) for
the threat model and how to report a vulnerability.

## Status and limitations

Version 0.1.0. Verified locally on macOS with Python 3.12 and 3.14, the .NET 10 SDK, and Node.js
24, using unit tests and the end-to-end example. CI runs on Linux and macOS with Python 3.10, 3.12,
and 3.14. Windows is not supported yet.

Not yet included: parallel execution, side-effect capture (files, databases, HTTP), stateful
sequences of calls, and adapters for other runtimes beyond the templates. Contributions are welcome.

## Related work

Record-and-replay tools such as [Keploy](https://keploy.io/) capture service traffic for
regression testing. Property-based testing libraries (Hypothesis, QuickCheck) inspired the
shrinking. portparity focuses on one narrow job: comparing a component against its replacement
across languages, inside an agent's workflow, with guardrails against evidence tampering.

## License

[MIT](LICENSE)
