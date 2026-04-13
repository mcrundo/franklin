# Changelog

All notable changes to franklin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `franklin run` crashed during map and reduce stages with `TypeError: '<' not supported between instances of 'OptionInfo' and 'int'`. Typer `Option` defaults are `OptionInfo` objects that only resolve to real values through CLI parsing — calling the functions directly from `run_pipeline` leaked the unresolved objects. Standalone `franklin map` and `franklin reduce` were unaffected.

## [0.4.0] - 2026-04-12

### Added

- `franklin publish <run>` — interactive guided publishing: grade check with auto-fix offer, editable repo name (default from plugin name), owner picker (personal account + orgs from `gh auth`), visibility picker, push, and install command printout.
- `franklin run --publish` and `franklin pick --publish` — wire the publish flow into the end of the pipeline for a true one-command experience: book file to published GitHub repo.
- `franklin diff <run-a> <run-b>` — compare two runs side-by-side: overall grade delta, per-artifact score changes with specific checks that fixed or regressed, content size comparison, and cost comparison.
- `franklin batch book1.epub book2.epub ...` — process multiple books sequentially with all gates auto-confirmed. Summary table with grade and cost per book at the end. Supports `--clean`.
- `franklin validate <run-dir>` — quick pre-publish rubric check on generated artifacts without full re-grading. Reports specific failed checks per artifact.
- `franklin stats` — aggregate dashboard: total books, completion rates, average grade, grade distribution, total and per-book cost.
- `franklin map --concurrency N` and `franklin reduce --concurrency N` — tunable parallelism for API tier limits (defaults: 8 map, 3 reduce).
- Auto-clean PDF suggestion: `franklin run` on a PDF without `--clean` now prompts to enable cleanup for better extraction quality.
- Plan stage shows an aesthetic spinner during the Opus call (was silent for 30-60s).
- `franklin doctor` checks `gh auth status` and warns if not authenticated.
- Assembled plugins include a `.gitignore` (pycache, .DS_Store, .env).
- Smarter error recovery: failed pipeline stages now print the exact retry command.
- 6 new smoke tests for diff, validate, stats, costs commands (388 total).

### Changed

- Run directory slugs now derived from book metadata title (EPUB OPF or PDF metadata) instead of the filename. Capped at 60 chars with word-boundary truncation. A 173-char PDF filename slug becomes ~47 chars from the clean title.
- Agent grading rubric expanded from 6 to 10 checks: structured checklist table, severity tiers, "Fix these first" guidance, and Output format section.
- Command grading rubric adds description-length check (under 80 chars).
- Cleanup cost tracking now records actual input/output token counts instead of zeros.
- README fully rewritten: leads with zero-touch flow, documents all 18 commands, explains both gates, covers batch mode, stats, costs, concurrency flags, and auto-clean.

### Fixed

- Repo names validated against strict alphanumeric pattern before subprocess calls (security hardening).
- Subprocess stderr sanitized for GitHub tokens (ghp_, gho_, github_pat_) before inclusion in error messages.

## [0.3.0] - 2026-04-12

### Added

- `franklin fix <run-dir>` command for interactive re-grading: grades the run, shows artifacts below threshold, offers Regenerate all / Pick / Done, re-runs reduce on selected artifacts, re-assembles, loops until satisfied.
- `franklin costs` command shows actual API spend across all runs with per-run table and per-stage breakdown. Supports `--json`.
- Cost tracking: each paid stage (cleanup, map, plan, reduce) now appends token counts and USD cost to `costs.json` in the run directory. Persists across resumes.
- Gate 2: post-map summary before the Opus plan call. Shows per-chapter extraction counts, cross-chapter concepts, top anti-patterns, and estimated plan+reduce cost. Proceed / Cancel prompt.
- `franklin assemble` generates a GitHub-ready `README.md` in the plugin root with install section, commands table, agents list, and reference index.
- `franklin push` now patches the README install section with the real `owner/repo` after a successful push.
- Async test coverage for `extract_chapter_async` and `generate_artifact_async`.

### Changed

- Reference prompt requires narrative evolution (show code progressing through stages, not just before/after).
- Agent prompt requires structured `| Check | Signal | Severity |` checklist and severity-weighted output with "Fix these first" section for reviewer agents.
- Command briefs must start with action verbs and frontmatter descriptions capped at 80 characters.
- SKILL.md prompt requires anti-pattern quick reference table and "Where does this code go?" routing table when book data supports them.
- Planner reference count guidance tightened from "15 beat 40" to "aim for 10-15".
- `bin/release --tag` now creates the GitHub Release from the CLI, bypassing the GITHUB_TOKEN limitation.

### Removed

- `.github/workflows/release.yml` — redundant now that `bin/release --tag` creates the GitHub Release directly.

## [0.2.1] - 2026-04-12

### Added

- `bin/release` script automates the entire release flow: version bump, changelog cut, sanity gate, commit, tag, push. See `docs/releasing.md`.
- Tag-triggered GitHub Actions workflow (`.github/workflows/release.yml`) creates a GitHub Release with changelog notes when a `v*.*.*` tag is pushed.
- Homebrew auto-bump workflow (`.github/workflows/homebrew-bump.yml`) waits for PyPI, fetches the new sdist URL + sha256, and opens a PR on the `mcrundo/homebrew-franklin` tap.
- Homebrew tap is live: `brew tap mcrundo/franklin && brew install franklin-book`.

### Changed

- Map and reduce stages now run concurrently via `AsyncAnthropic` with bounded semaphores. Map defaults to 8 in-flight, reduce to 3. A 26-chapter PDF pipeline drops from ~74 min to ~20 min wall clock.
- Cost estimate callout is now a Rich Panel explaining that estimates are budget ceilings, not predictions, with a CTA to report real costs.
- Reference prompt template now requires problem framing, "When to use", and code examples as mandatory structural sections (previously suggestions; their absence caused F grades).
- Plan prompt now explicitly enumerates valid `feeds_from` category names and forbids item-level IDs.

### Fixed

- Double "Next steps" block: `assemble` printed it, then `run_pipeline` printed it again.
- Stringified JSON recovery: LLMs sometimes return a JSON string where a list is expected (e.g. `anti_patterns` as `"[{...}]"`). The validator now deserializes these before Pydantic validation.
- Planner feed alignment: added an alias map in the resolver so common short names (`workflow` -> `actionable_workflows`, `concept` -> `concepts`, etc.) resolve correctly instead of appearing as unresolved feeds.
- YAML frontmatter repair: when the LLM puts unquoted colons in a description (e.g. Rails migration syntax `null: false`), the assembler now tries quoting scalar values before reporting a parse error.

## [0.2.0] - 2026-04-11

Picker UX overhaul, a pre-map confirmation gate, robustness fixes against LLM drift, and Homebrew distribution.

### Added

#### Picker
- `franklin pick` truncates long titles to fit the terminal and adds Author and Year columns so identically-titled books disambiguate. Metadata comes from a fast OPF read (zipfile + stdlib XML — no ebooklib, no chapter parsing) with filename parsing as a fallback for `Author - Title (Year)` patterns.
- Pre-map confirmation gate (Gate 1): after you pick a book, `franklin pick` runs ingest and shows a cost table before any paid stage starts. Options are **Proceed**, **Edit chapter selection**, or **Cancel**. "Edit" opens a multi-select with every content chapter pre-checked (spacebar to toggle, Enter to commit); the gate re-renders the estimate after edits so you see the cost impact before committing. Selection persists to `map_selection.json` and is honored by the map stage on resume.

#### Cost estimates
- Cost estimates display as a range (`$low - $high`) in both `franklin pick` and `franklin run --estimate`, with a methodology footer explaining the token heuristic, pricing, and which stage is free. The low end assumes prompt caching and realistic output lengths; the high end is the pessimistic worst case.

#### Distribution
- Homebrew tap support via `bin/bump-homebrew` (prints PyPI sdist URL + sha256 ready to paste into a formula) and `docs/homebrew.md` (one-time tap setup, formula maintenance, and rationale for not targeting homebrew-core yet).

### Changed

- `franklin run` skips the ingest stage when `book.json` already exists (same shape as the existing plan-skip), so the pick-flow gate's ingest isn't duplicated when the full pipeline runs after.

### Fixed

- `estimate_run` previously zipped chapters against `book.structure.toc` by position, which silently misaligned on partial manifests. It now matches by `chapter_id`.
- Map and plan stages no longer die on a single stray LLM field. A new shared `validate_with_extra_recovery` helper keeps the outgoing tool schemas strict (`additionalProperties: false`) but, on validation, strips `extra_forbidden` keys and retries — so an LLM slip like `source_quote` on a `Principle` doesn't blow up a chapter's extraction work or wipe out an entire plan call. Stripped fields are logged so drift stays visible.
- Reduce stage now warns when an artifact's `feeds_from` references chapters or fields that didn't resolve (planner hallucination, partial run, or a chapter the user deselected at Gate 1). Previously the unresolved paths were collected and silently ignored, shipping a degraded artifact without surfacing the missing context.

## [0.1.0] - 2026-04-11

First public release.

### Added

#### Pipeline
- Five-stage pipeline: `ingest → map → plan → reduce → assemble`, each a standalone CLI command that reads from and writes to a run directory so stages can be replayed independently.
- `franklin ingest` parses EPUB and PDF into `NormalizedChapter` JSON. PDF support is layout-aware via pdfplumber with an optional Tier 4 LLM cleanup pass (`--clean`) for mechanical artifact fixes.
- `franklin map` runs per-chapter structured extraction (concepts, principles, rules, anti-patterns, workflows, code examples) via Claude Sonnet.
- `franklin plan` uses Claude Opus as an architectural advisor to design the plugin layout — the high-leverage half of the advisor strategy pattern.
- `franklin reduce` generates each artifact file from the plan using Claude Sonnet — the cheap executor half. ~80% cost savings vs pure Opus for the same output quality.
- `franklin assemble` writes `plugin.json`, runs link/frontmatter/template validators, and produces a grade card plus `metrics.json`.
- `franklin run` chains all five stages end-to-end with resume-on-disk semantics.

#### UX
- `franklin doctor` preflight check: Python version, `uv` binary, Anthropic API key resolution, license state, `claude` CLI, network reachability, disk space. `--skip-network` and `--json` supported.
- `franklin pick` interactive book picker that scans a directory for `.epub` and `.pdf` files and cross-references existing run state (new / partial / assembled with grade).
- `franklin runs list` table view of every run directory with slug, title, date, last completed stage, and grade.
- `franklin grade <run-dir>` standalone diagnostic with detailed per-artifact rubric breakdown. `--json` for tooling.
- `franklin review <run-dir>` interactive plan pruning: omit artifacts by index or range (`1,3-5`) before paying for reduce. Also available as `franklin run --review` for a mid-pipeline pause.
- `franklin run --estimate` pure-heuristic cost preview from a parsed `BookManifest` without touching the paid stages.
- Resume detection on `franklin run`: partial run directories show per-stage progress and prompt to continue from the first incomplete stage. `--yes` auto-confirms, `--force` restarts.
- Interactive book metadata confirmation after ingest catches wrong EPUB titles before they turn into plugin identifiers.
- Live Rich progress bars on map, reduce, and cleanup with spinner, count, elapsed, ETA, and current item. Final summaries include actual USD cost.
- Tailored "next steps" block after `franklin run` and `franklin assemble` guides users to local install, publish, or iteration commands.
- Friendly error formatter classifies Anthropic SDK errors (rate limit, auth, 529, connection, timeout) and Franklin errors (missing API key, license, unsupported format) into actionable blocks with suggested next steps.

#### Performance
- Async Tier 4 cleanup via `AsyncAnthropic` with bounded semaphore. A 29-chapter book drops from ~50 minutes sequential to ~6 minutes at the default `--clean-concurrency=8`.

#### Publishing
- `franklin push` publishes an assembled plugin tree to a GitHub repository with optional PR creation (`--pr`) and public/private visibility (`--public`).
- `franklin install` installs an assembled plugin into Claude Code at `user`, `project`, or `local` scope.

#### License infrastructure (disabled in v0.1)
- RS256 JWT license module (`franklin license {login, logout, whoami, status}`) with offline grace window, cached revocations, and bypass env var. Gate code is wired but disabled via `_LICENSE_GATE_ENABLED = False` so v0.1 ships fully free. The flag stays in place for a future paid tier.

[Unreleased]: https://github.com/mcrundo/franklin/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/mcrundo/franklin/releases/tag/v0.4.0
[0.3.0]: https://github.com/mcrundo/franklin/releases/tag/v0.3.0
[0.2.1]: https://github.com/mcrundo/franklin/releases/tag/v0.2.1
[0.2.0]: https://github.com/mcrundo/franklin/releases/tag/v0.2.0
[0.1.0]: https://github.com/mcrundo/franklin/releases/tag/v0.1.0
