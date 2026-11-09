# Contributing

Thanks for helping. Bug reports, adapter templates for more runtimes, and real-world migration
examples are especially welcome.

## Ground rules

- **Standard library only** in `skills/portparity/scripts/portparity.py`. The skill must run anywhere Python 3.10+ runs, with no install step.
- **Every change ships with a test.** `python3 -m unittest discover -s tests -v` must pass. The
  end-to-end example tests need the .NET 10 SDK and Node.js; they are skipped if those are missing.
- **Fail closed.** Unknown config keys, unsupported schema keywords, and malformed responses are errors, not warnings.
- **Never weaken a guardrail silently.** Changes that affect what counts as pass, or how untrusted
  output is handled, need a note in `SECURITY.md` and `CHANGELOG.md`.
- **Keep `SKILL.md` short.** Put detail in `references/`. Agents load `SKILL.md` on every use.
- **Be precise about claims.** Docs and reports must not imply that passing cases prove equivalence.

## Development

```sh
python3 -m unittest discover -s tests -v
cd examples/shipping-fee && dotnet build original -c Release -o original/out
python3 ../../skills/portparity/scripts/portparity.py run portparity.json --out /tmp/pp
```

Validate the plugin manifests before a release:

```sh
claude plugin validate .
```

## Releases

1. Update `VERSION` in `portparity.py`, `version` in `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`, and `CHANGELOG.md`.
2. Tag `vX.Y.Z` on a commit where CI is green.

By contributing you agree that your contributions are licensed under the MIT License.
