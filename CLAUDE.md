# Franklin

Turns technical books (EPUB) into Claude Code plugins via a staged pipeline: **ingest → map → plan → reduce → assemble**.

## Stack

- Python 3.12, managed with `uv`
- Typer CLI (`franklin`), Pydantic v2 models, Rich output
- Anthropic SDK for LLM calls, tenacity for retries
- ruff (line-length 99), mypy strict, pytest

## Commands

```bash
uv sync                      # install
uv run franklin ingest BOOK  # run ingest stage
uv run pytest                # tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # type check
```

Run directories land in `./runs/<slug>/` — `book.json` plus `raw/chNN.json` per chapter. Ingest is deterministic, no LLM calls.

## Layout

- `src/franklin/cli.py` — Typer entrypoint
- `src/franklin/ingest/` — EPUB parsing
- `src/franklin/llm/` — Anthropic client wrapper, prompt loader, prompts as markdown
- `src/franklin/models/` — Pydantic schemas (chapters, extractions)
- `tests/` — pytest

## Working preferences

- Use `uv run` for anything Python — never bare `python` or `pip`.
- Keep mypy strict-clean and ruff-clean before declaring a task done.
- Prefer editing existing files; don't create new modules speculatively.
- Prompts live as markdown under `src/franklin/llm/prompts/` and are loaded by the prompt loader — add new ones there, don't inline strings.
- Pydantic models are the contract between stages. When changing a stage's output, update the model first.
- Don't commit unless asked. When asked, write terse commit messages matching the existing log style (`Add X`, `Classify Y`, imperative, no body unless needed).

## Autonomy

This repo is configured for long-running autonomous work: edits auto-accept, common `uv`/`git`/test commands are pre-allowed, and network/destructive commands are denied. If you hit something outside the allowlist, stop and ask rather than working around it.

When given a multi-step task: keep going until tests pass, lint is clean, and the definition of done is met. Only stop to ask if genuinely blocked (ambiguous requirements, destructive action needed, external credential required).
