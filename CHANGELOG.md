# Changelog

All notable changes to franklin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Map stage no longer dies on a single stray LLM field. The `ChapterExtraction` validator stays strict on required fields and types, but now strips `extra_forbidden` keys (and logs them) before retrying — so an LLM slip like `source_quote` on a `Principle` doesn't blow up an entire chapter's extraction work.

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

[Unreleased]: https://github.com/mcrundo/franklin/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mcrundo/franklin/releases/tag/v0.1.0
