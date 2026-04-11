# Franklin

Turn technical books into Claude Code plugins.

Franklin reads an EPUB or PDF and produces a full Claude Code plugin — SKILL, reference tree, slash commands, subagents, and plugin packaging — by extracting the book's concepts, principles, and workflows into structured intermediate data, then generating one artifact at a time. Every stage is a separate CLI command so you can iterate cheaply, and every stage writes to disk so crashes resume where they stopped.

## Install

```bash
uv sync
```

Franklin is a Python 3.12+ package managed with `uv`. All commands below assume `uv run franklin …`; drop the `uv run` prefix if you install the package globally.

## First-run checklist

```bash
uv run franklin doctor
```

`franklin doctor` is a preflight: it verifies your Python version, `uv` and `claude` binaries, Anthropic API key resolution, license state, network reachability to `api.anthropic.com`, and available disk space. Run it once after install to catch setup issues before your first paid run. `--skip-network` for air-gapped environments, `--json` for support tooling.

## Anthropic API key

Franklin looks up `ANTHROPIC_API_KEY` in the environment first, then falls back to the OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service) under service name `franklin`:

```bash
# Option 1: environment variable (CI, direnv, 1Password op run)
export ANTHROPIC_API_KEY=sk-ant-...

# Option 2: OS keychain (local dev — stored encrypted at rest)
keyring set franklin ANTHROPIC_API_KEY
```

Use whichever fits your workflow. You do not need to configure anything in Franklin itself.

## The happy path

```bash
# Optional: pick a book interactively from ~/Downloads
uv run franklin pick

# Or point directly at a file
uv run franklin run path/to/book.epub

# Curious what it'll cost first? Dry-run the estimator.
uv run franklin run path/to/book.epub --estimate

# Want a pause between plan and reduce to prune artifacts?
uv run franklin run path/to/book.epub --review
```

`franklin run` chains the five pipeline stages end-to-end (`ingest → map → plan → reduce → assemble`) and ends with a grade card plus a tailored "next steps" block. It's resume-safe: re-running over an existing run directory detects which stages are done and prompts to continue from the first incomplete one. Use `--force` to restart from scratch, `--yes` to auto-confirm prompts in scripts.

## Pipeline stages

Every stage can be run on its own, reads from disk, and writes to disk — so you can replay just one stage when you change a prompt or want to inspect intermediate state.

1. **ingest** (`franklin ingest <book>`) — parse EPUB/PDF into normalized chapter JSON. Deterministic, no LLM calls. PDFs get an optional Tier 4 LLM cleanup pass via `--clean` that fixes extraction artifacts concurrently with a live progress bar.
2. **map** (`franklin map <run-dir>`) — per-chapter structured extraction. One LLM call per chapter produces a sidecar with concepts, rules, anti-patterns, and workflows.
3. **plan** (`franklin plan <run-dir>`) — design the plugin architecture from the distilled sidecars (one call).
4. **reduce** (`franklin reduce <run-dir>`) — generate each artifact file from the plan. This is the most expensive stage.
5. **assemble** (`franklin assemble <run-dir>`) — write `plugin.json`, run link/frontmatter/template validators, and compute the grade card.

## Iteration tools

- **`franklin runs list`** — table of every run directory under `./runs/` with slug, title, date, last completed stage, artifact count, and grade.
- **`franklin grade <run-dir>`** — detailed per-artifact grade report with structural rubric scores, lowest-grade artifacts, and suggested regeneration commands. `--json` for machine output.
- **`franklin review <run-dir>`** — interactive pruning of the planned artifact list. Show the plan, omit artifacts you don't want to pay to generate, save a reduced `plan.json`. Supports index ranges like `1,3-5`.
- **`franklin inspect <run-dir>`** — preview the ingest output (chapters, code blocks, anomalies) before committing to the paid stages.
- **`franklin reduce <run-dir> --artifact <id> --force`** — regenerate one artifact after editing a prompt or fixing a sidecar.

## Publishing and installing

Once a run is assembled and you're happy with the grade:

```bash
# Try it locally before publishing (ephemeral, per-session)
uv run franklin install <run-dir> --scope local

# Or persist it to your user scope (every Claude Code session)
uv run franklin install <run-dir> --scope user

# Or scope it to the current project (committed to .claude/settings.json)
uv run franklin install <run-dir> --scope project

# When ready to share, push to a GitHub repo
uv run franklin push <run-dir> --repo owner/name

# Other users (and you, after publishing) install from GitHub
claude plugin install owner/name
```

v0.1 ships fully free: every command above is available without a license, and you don't need to run `franklin license login` before `franklin push` or `franklin install`. The license module stays in place for a future paid tier; `franklin license status` continues to work if you want to inspect state, but no command calls it as a gate today.

## Advisor strategy (Opus advises, Sonnet executes)

Franklin uses the [advisor pattern](https://algoinsights.medium.com/the-advisor-strategy-how-to-get-claude-opus-intelligence-without-opus-prices-bfd17bbed96b) to get Opus-quality output at roughly Sonnet prices: a single expensive Opus call produces a high-leverage *plan*, and many cheap Sonnet calls *execute* that plan one artifact at a time.

Concretely:

- **`plan` (Opus)** runs once per run and outputs `plan.json` — the full plugin architecture, artifact list, and per-file `feeds_from` wiring. This is the advisory call; it thinks holistically about the book and the plugin shape.
- **`reduce` (Sonnet)** runs N times — once per artifact — and generates each file using the brief Opus wrote. Sonnet never has to reason about the architecture; it just fills in a well-scoped plan.

For a typical 28-artifact run that translates to **1× Opus + 28× Sonnet** instead of **29× Opus**. In dollar terms that's roughly `$0.30 + 28 × $0.20 ≈ $5.90` vs `29 × $1.00 ≈ $29.00` — the same output quality for about 20% of the cost. `map` and `cleanup` similarly use Sonnet since they're per-chapter executions of a well-defined extraction prompt, not architectural decisions.

The tradeoff is that Opus's advisory call is a single point of failure: if the plan is wrong, all 28 reduces inherit the mistake. `franklin review <run-dir>` pauses between plan and reduce so you can prune or redirect the plan before paying for execution — that's the human-in-the-loop half of the pattern.

## Cost and performance

- **`franklin run --estimate`** predicts per-stage token counts and dollar cost from a parsed `BookManifest` before any paid calls. Lean pessimistic — real runs should come in at or below the estimate.
- **Tier 4 cleanup** uses `AsyncAnthropic` with a bounded semaphore (`--clean-concurrency=8` default). A 29-chapter book drops from ~50 minutes sequential to ~6 minutes.
- **Resume-on-disk** means a flaky network or a burned credit card never costs you more than the stage that was in flight. Re-run the same command; Franklin picks up from `book.json`, `chapters/`, `plan.json`, or `output/` depending on what already exists.
- **Per-chapter failures are non-fatal** during cleanup and map; the original Tier 2 output is kept and reported in the summary.

## Run directory layout

```
runs/<slug>/
  book.json              # BookManifest (evolves across stages)
  raw/chNN.json          # NormalizedChapter — one per chapter from ingest
  chapters/chNN.json     # ChapterSidecar — one per chapter from map
  plan.json              # PlanManifest — from plan
  output/<plugin>/       # Generated plugin tree — from reduce/assemble
    .claude-plugin/plugin.json
    skills/<name>/SKILL.md
    references/.../*.md
    commands/*.md
    agents/*.md
  metrics.json           # Grade card output from assemble
```

## Configuration

- **Runs directory**: defaults to `./runs/` relative to cwd; override with `--output` on any stage command.
- **License directory**: defaults to `~/.config/franklin/`; override with `FRANKLIN_LICENSE_DIR`.
- **Models**: `claude-sonnet-4-6` is the default for `map`, `reduce`, and `cleanup`; `claude-opus-4-6` is the default for `plan`. Override per-command with `--model`.

## Development

```bash
uv run pytest              # run the test suite
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy                # type check (strict)
```

Prompts live as markdown under `src/franklin/llm/prompts/` and are loaded by the prompt renderer — add new ones there rather than inlining strings.
