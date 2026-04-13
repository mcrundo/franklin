# Franklin

Turn technical books into Claude Code plugins.

Franklin reads an EPUB or PDF and produces a full Claude Code plugin — SKILL, reference tree, slash commands, subagents, and plugin packaging — by extracting the book's concepts, principles, and workflows into structured intermediate data, then generating one artifact at a time. Every stage is a separate CLI command so you can iterate cheaply, and every stage writes to disk so crashes resume where they stopped.

## Install

The easiest install is via [`uv`](https://docs.astral.sh/uv/) or `pipx`:

```bash
uv tool install franklin-book
# or
pipx install franklin-book
```

On macOS / Linux you can also install via Homebrew:

```bash
brew tap mcrundo/franklin
brew install franklin-book
```

All three drop a `franklin` command onto your PATH. (The distribution is called `franklin-book` on PyPI and Homebrew because `franklin` was already taken; the CLI you actually type is still `franklin`. See `docs/homebrew.md` for tap maintenance details.)

For development from a clone:

```bash
uv sync
uv run franklin doctor
```

Franklin is a Python 3.12+ package managed with `uv`.

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
# Zero-touch: book file → assembled + published plugin
uv run franklin run path/to/book.epub --publish

# Interactive: pick a book, preview costs, select chapters, then run
uv run franklin pick

# Pick and publish in one flow
uv run franklin pick --publish

# Just build, publish later
uv run franklin run path/to/book.epub
uv run franklin publish runs/<slug>

# Curious what it'll cost first?
uv run franklin run path/to/book.epub --estimate

# Process a whole library at once
uv run franklin batch book1.epub book2.epub book3.pdf --clean
```

The pipeline has two interactive gates that pause for confirmation:

- **Gate 1** (pre-map): shows a cost estimate with a low-high range, lets you edit the chapter selection (spacebar to toggle, Enter to commit). Persists via `map_selection.json`.
- **Gate 2** (post-map, pre-plan): shows what the map extracted — per-chapter counts, cross-chapter concepts, top anti-patterns — so you can verify quality before the expensive Opus plan call.

`franklin run` chains five stages end-to-end (`ingest → map → plan → reduce → assemble`) and is resume-safe: re-running picks up from the first incomplete stage. Use `--force` to restart, `--yes` to auto-confirm in scripts.

## Pipeline stages

Every stage can be run on its own, reads from disk, and writes to disk — so you can replay just one stage when you change a prompt or want to inspect intermediate state.

1. **ingest** (`franklin ingest <book>`) — parse EPUB/PDF into normalized chapter JSON. Deterministic, no LLM calls. PDFs get an optional Tier 4 LLM cleanup pass via `--clean` that fixes extraction artifacts concurrently with a live progress bar.
2. **map** (`franklin map <run-dir>`) — per-chapter structured extraction. One LLM call per chapter produces a sidecar with concepts, rules, anti-patterns, and workflows.
3. **plan** (`franklin plan <run-dir>`) — design the plugin architecture from the distilled sidecars (one call).
4. **reduce** (`franklin reduce <run-dir>`) — generate each artifact file from the plan. This is the most expensive stage.
5. **assemble** (`franklin assemble <run-dir>`) — write `plugin.json` and `README.md`, run link/frontmatter/template validators, and compute the grade card. The generated README is GitHub-ready with install instructions, commands table, and reference index.

## Iteration tools

- **`franklin fix <run-dir>`** — interactive re-grade loop. Shows artifacts below B, offers to regenerate all or pick specific ones, re-runs reduce + assemble, shows the new grade. Loops until you're satisfied.
- **`franklin validate <run-dir>`** — quick quality check without re-grading. Catches common prompt-compliance issues (missing problem framing, long command descriptions, agents without checklists).
- **`franklin diff <run-a> <run-b>`** — compare two runs side-by-side: grade delta, per-artifact score changes, which checks fixed or regressed, content size and cost comparison.
- **`franklin grade <run-dir>`** — detailed per-artifact grade report with structural rubric scores, lowest-grade artifacts, and suggested regeneration commands. `--json` for machine output.
- **`franklin costs`** — actual API spend across all runs with per-run and per-stage breakdown.
- **`franklin runs list`** — table of every run directory with slug, title, date, last completed stage, and grade.
- **`franklin review <run-dir>`** — interactive pruning of the planned artifact list before reduce.
- **`franklin inspect <run-dir>`** — preview the ingest output before committing to the paid stages.
- **`franklin reduce <run-dir> --artifact <id> --force`** — regenerate one artifact.

## Publishing and installing

```bash
# Interactive publish: grade check, fix low artifacts, pick repo name + owner
uv run franklin publish <run-dir>

# Or do it all in one shot from a book file
uv run franklin run path/to/book.epub --publish

# Try it locally before publishing
uv run franklin install <run-dir> --scope local

# Other users install from GitHub
claude plugin add owner/repo
```

`franklin publish` walks you through repo naming (editable default from the plugin name), owner selection (personal account or org, from `gh auth`), and visibility — then pushes and prints the install command.

### Batch processing

Process multiple books at once:

```bash
uv run franklin batch book1.epub book2.epub book3.pdf --clean
```

Each book gets its own run directory. All gates are auto-confirmed. A summary table with grades and costs is printed at the end.

## Advisor strategy (Opus advises, Sonnet executes)

Franklin uses the [advisor pattern](https://algoinsights.medium.com/the-advisor-strategy-how-to-get-claude-opus-intelligence-without-opus-prices-bfd17bbed96b) to get Opus-quality output at roughly Sonnet prices: one Opus call produces a high-leverage *plan*, and many cheap Sonnet calls *execute* it.

- **`plan` (Opus)** runs once and outputs `plan.json` — the full plugin architecture, artifact list, and per-file `feeds_from` wiring. This is the advisory call; it thinks holistically about the book and the plugin shape.
- **`reduce` (Sonnet)** runs N times — once per artifact — and generates each file from the brief Opus wrote. Sonnet never has to reason about the architecture; it just fills in a well-scoped plan.
- **`map` and `cleanup`** also use Sonnet — they're per-chapter executions of a well-defined extraction prompt, not architectural decisions.

The result: a typical 30-artifact book costs **$3–5 in API spend** (one Opus plan call + ~30 Sonnet calls), with most of the savings coming from Anthropic's prompt caching (90% discount on repeated input tokens across chapters). `franklin run --estimate` shows a pessimistic budget ceiling before any paid calls — real costs are typically well below it.

The tradeoff is that Opus's advisory call is a single point of failure: if the plan is wrong, every reduce inherits the mistake. `franklin review <run-dir>` pauses between plan and reduce so you can prune or redirect the plan before paying for execution.

## Cost and performance

- **`franklin run --estimate`** predicts per-stage token counts and dollar cost before any paid calls. Displayed as a `$low - $high` range — a budget ceiling, not a prediction. Real costs are typically 30–50% below the high end thanks to prompt caching.
- **`franklin pick` shows the same estimate** as a pre-map gate so you can deselect chapters before paying for them.
- **Concurrent stages.** Map, reduce, and cleanup all run concurrently via `AsyncAnthropic` with bounded semaphores. A 26-chapter PDF goes from ingest to assembled plugin in ~20 minutes.
- **Resume-on-disk** means a flaky network or a burned credit card never costs you more than the calls that were in flight. Re-run the same command; Franklin picks up from `book.json`, `chapters/`, `plan.json`, or `output/` depending on what already exists.
- **Per-chapter failures are non-fatal** during cleanup and map; the original output is kept and reported in the summary.
- **LLM drift is tolerated**, not fatal. If a tool-use response slips a stray field onto a sub-object, the validator strips it and logs a warning instead of killing the whole stage. Missing required fields and type errors still raise.

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
