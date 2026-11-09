# Changelog

All notable changes are documented here. The project follows [Semantic Versioning](https://semver.org/).

## 0.1.0 - 2026-11-09

First release.

- Agent skill (`SKILL.md`) with a workflow, guardrails, stop conditions, and troubleshooting for behavior-preserving migrations.
- Dependency-free runner: `run`, `validate`, and `generate` commands.
- JSON-lines adapter protocol with `process` and `stream` modes, timeouts, process-group cleanup, and output limits.
- Strict JSON comparison with optional float tolerances and configurable error fields.
- Failure-preserving shrinking, once per failure type, with regression case output.
- Negative controls, determinism checks, accepted differences that need named approval, and stale-acceptance warnings.
- Seeded case generator for a JSON Schema subset, with edge-value bias.
- Adapter templates for Python, Node.js, and .NET.
- Example: a legacy C# rule, a correct Python port, and a broken JavaScript port.
- Claude Code and Codex plugin manifests with an icon.
- Project files: README with banner and FAQ, `CITATION.cff`, code of conduct, issue and pull request templates, `llms.txt`.
