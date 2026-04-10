# Franklin

Turn technical books into Claude Code plugins.

Franklin reads an EPUB and produces a full Claude Code plugin — skill, reference tree, slash commands, subagents, and plugin packaging — by extracting the book's concepts, principles, and workflows into structured intermediate data, then generating one artifact at a time.

## Status

v0.1 scaffold. Schema and EPUB ingest are working. Map, plan, reduce, and assemble stages are stubs.

## Install

```bash
uv sync
```

## Anthropic API key

Franklin looks up `ANTHROPIC_API_KEY` in the environment first, then falls back to the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) under service name `franklin`. The same code works for every way you might want to provide a key:

```bash
# Option 1: environment variable (CI, direnv, 1Password op run)
export ANTHROPIC_API_KEY=sk-ant-...

# Option 2: OS keychain (local dev — stored encrypted at rest)
keyring set franklin ANTHROPIC_API_KEY
```

Use whichever fits. You do not need to configure anything in Franklin itself.

## Usage

```bash
franklin ingest path/to/book.epub
franklin map runs/<slug> --chapter ch06
franklin map runs/<slug>
```

`franklin ingest` writes a run directory at `./runs/<slug>/` containing `book.json` and one `raw/chNN.json` per chapter. No LLM calls.

`franklin map <run_dir>` runs per-chapter structured extraction via the Anthropic API. `--chapter ch06` targets a single chapter for iteration, `--dry-run` prints the prompt without calling the API, and `--force` re-extracts chapters that already have sidecars on disk.

## Pipeline stages

1. **ingest** — EPUB to normalized chapter JSON (deterministic, no LLM).
2. **map** — per-chapter structured extraction (concepts, rules, anti-patterns, workflows, etc).
3. **plan** — propose the plugin layout (skill, references, commands, agents) with human review.
4. **reduce** — generate one file per artifact.
5. **assemble** — package the plugin with `plugin.json` and link-check references.
