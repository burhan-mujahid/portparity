# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/burhan-mujahid/portparity/security/advisories/new).
Do not open a public issue. Include the version, platform, a minimal reproduction, and the impact.
Expect an acknowledgement within 7 days. Fixes are released as patch versions and noted in the changelog.

Only the latest release is supported.

## Threat model

portparity executes the commands in a config file, as the invoking user, on inputs from case files.

**Trusted:** the config file, the case files, the adapters, and the machine running portparity. A
config is equivalent to a script. Review configs from others before running them.

**Untrusted:** everything the tested programs write (stdout, stderr, exit status, timing). The code
under test may be generated, buggy, or hostile in its output. AI agents reading reports are a
specific concern, because output could contain text written to manipulate them.

| Risk | Mitigation | Test |
| --- | --- | --- |
| Shell injection through commands or inputs | Commands are argument arrays run with `shell=False`. Inputs go only through stdin | config tests reject string commands |
| Secrets leaking into tested processes | Environment rebuilt from an allowlist. Config `env` values are never written to reports, only key names | `test_environment_is_scrubbed` |
| Runaway or hung processes | Per-request timeout; new session and process-group kill; stdin written from a separate thread | `test_timeout_kills_process_group`, `test_adapter_that_never_reads_stdin_cannot_block_the_runner` |
| Memory exhaustion through output | Per-line stdout cap, stderr tail cap, case size and count limits, JSON depth limit | `test_replacement_failures_are_mismatches`, `test_stderr_is_drained_and_capped` |
| Malformed or ambiguous JSON | Strict RFC 8259 parsing: no NaN or Infinity, no overflow to infinity, no duplicate keys, no lone surrogates | `StrictJsonTests` |
| Line-splitting attacks (U+2028/U+2029) | All requests and JSONL files are ASCII-escaped | `test_wire_format_is_ascii` |
| Terminal or Markdown injection, Trojan Source | Control and bidi-override characters escaped; output fenced with a fence longer than any backtick run; Markdown report labels output as untrusted | `test_untrusted_output_is_escaped_in_reports` |
| Report tampering, or clobbering files through symlinks | Output directory created 0700; files written 0600 via atomic rename; symlinks refused; `--force` limited to previous portparity runs | `OutputSafetyTests` |
| A harness that cannot fail | Negative controls must be rejected (exit 4 otherwise); nondeterminism reported as `unstable`; oracle failures reported as inconclusive, never pass | `test_negative_controls`, `test_nondeterminism_is_detected` |

## Out of scope

- **Sandboxing.** portparity does not restrict file system, network, or system-call access. Run
  untrusted code, generated code you have not reviewed, or code with side effects inside a
  container or VM with no production credentials.
- **Resource limits** other than time and output size (CPU, memory, disk). Use container limits or `ulimit`.
- **Windows.** Process-group termination and file-permission guarantees are implemented and tested for Linux and macOS only.
- **Correctness proofs.** A passing run is evidence for the listed cases only.

## Supply chain

The runner uses only the Python standard library. CI actions are pinned to full commit SHAs,
checkout does not persist credentials, the workflow has read-only permissions, and Dependabot
proposes action updates after a 14-day cooldown.
