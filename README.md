# Franklin

Turn technical books into Claude Code plugins.

Franklin reads an EPUB and produces a full Claude Code plugin — skill, reference tree, slash commands, subagents, and plugin packaging — by extracting the book's concepts, principles, and workflows into structured intermediate data, then generating one artifact at a time.

## Status

v0.1 scaffold. Schema and EPUB ingest are working. Map, plan, reduce, and assemble stages are stubs.

## Install

```bash
uv sync
```

## Usage

```bash
franklin ingest path/to/book.epub
```

Writes a run directory at `./runs/<slug>/` containing `book.json` and one `raw/chNN.json` per chapter. No LLM calls are made during ingest.

## Pipeline stages

1. **ingest** — EPUB to normalized chapter JSON (deterministic, no LLM).
2. **map** — per-chapter structured extraction (concepts, rules, anti-patterns, workflows, etc).
3. **plan** — propose the plugin layout (skill, references, commands, agents) with human review.
4. **reduce** — generate one file per artifact.
5. **assemble** — package the plugin with `plugin.json` and link-check references.
