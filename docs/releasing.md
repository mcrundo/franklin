# Releasing Franklin

Releases are mostly automated. The only manual command is one shell script.

## TL;DR

```bash
bin/release 0.3.0        # bump, open release PR
# merge the PR on GitHub
bin/release 0.3.0 --tag  # tag merge commit, trigger publish chain
```

Two commands, one PR merge in between. Everything downstream — GitHub Release, PyPI upload, Homebrew tap PR — fires automatically from the tag.

## What `bin/release` does

### Phase 1: `bin/release X.Y.Z` (prepare)

1. **Preflight.** Verifies semver format, clean working tree, you're on `main`, `main` is in sync with `origin/main`, the `[Unreleased]` CHANGELOG section has actual content, the version is different from the current one, and no `vX.Y.Z` tag exists locally or on origin.
2. **Version bump.** Edits `version` in `pyproject.toml`, `__version__` in `src/franklin/__init__.py`, and runs `uv lock` to refresh the lockfile.
3. **Changelog cut.** Inserts a `## [X.Y.Z] - YYYY-MM-DD` heading right below `## [Unreleased]`, leaving the existing entries to fall under the new dated header. Updates the link references at the bottom.
4. **Sanity gate.** Runs `ruff check`, `ruff format --check`, `mypy`, and `pytest -q`. Fails fast if any check is red.
5. **Commit on release branch.** Creates `release/X.Y.Z`, commits `Release X.Y.Z`, pushes the branch, opens a PR via `gh`, then resets local main to stay in sync with origin.

### Phase 2: `bin/release X.Y.Z --tag` (after PR merge)

1. Pulls latest main (which now has the release commit).
2. Verifies `pyproject.toml` version matches X.Y.Z.
3. Tags `vX.Y.Z` on HEAD and pushes the tag.

### Flags

- `--dry` — do everything except commit/push (useful for inspecting the diff)
- `--yes` — skip confirmation prompts
- `--direct` — old behavior: push to main directly (needs admin bypass on protected branches)
- `--tag` — phase 2: tag the merge commit after the release PR is merged

## What happens after `git push origin v0.3.0`

The tag push fans out into three workflows:

```
   git push origin v0.3.0
            |
            v
   .github/workflows/release.yml      ← tag-triggered
            |
            | (verifies pyproject + __init__ match the tag,
            |  extracts the matching CHANGELOG section,
            |  creates the GitHub Release with those notes)
            v
   GitHub Release "v0.3.0"            ← release-triggered
            |
   +--------+--------+
   |                 |
   v                 v
publish.yml     homebrew-bump.yml
   |                 |
   | (build sdist    | (waits for PyPI to settle,
   |  + wheel,       |  fetches new sdist URL + sha256,
   |  upload to      |  patches Formula/franklin-book.rb in
   |  PyPI via       |  mcrundo/homebrew-franklin, opens a
   |  OIDC)          |  PR on the tap repo)
   v                 v
PyPI 0.3.0       Tap PR
```

You then review and merge the tap PR. The formula uses pip with pre-built wheels (not source builds), so dependency changes are resolved automatically — just smoke-test with `brew install franklin-book && brew test franklin-book` before merging.

## Required secrets

| Secret | Used by | Purpose |
|---|---|---|
| `HOMEBREW_TAP_TOKEN` | `homebrew-bump.yml` | Fine-grained PAT scoped to `mcrundo/homebrew-franklin` with **Contents: Read and write** + **Pull requests: Read and write**. Configure at https://github.com/settings/tokens?type=beta and add to repo Actions secrets. |

PyPI publishing already uses [OIDC trusted publishing](https://docs.pypi.org/trusted-publishers/) so it doesn't need a token.

## Manual fallbacks

If something breaks, every step has a manual escape hatch:

- **Version bump didn't take** → `bin/release` exits before commit on any failure. Fix the underlying issue (probably an unclean tree or out-of-sync main) and re-run.
- **Wrong version pushed** → delete the tag (`git push origin :refs/tags/vX.Y.Z`), bump correctly, re-tag, re-push. `bin/release` will refuse to bump to an existing tag.
- **release.yml failed** (tag-vs-pyproject mismatch, missing changelog section) → fix the issue, retag the same commit (`git tag -f vX.Y.Z && git push --force origin vX.Y.Z`), or create the GitHub Release manually with `gh release create vX.Y.Z --notes-file <(awk ...)`. The downstream workflows fire on `release: published` regardless of how the release was made.
- **publish.yml failed** → re-run from the GitHub Actions tab. OIDC has no credentials to expire.
- **homebrew-bump.yml didn't fire or failed** → run `bin/bump-homebrew` locally to get the new URL/sha256, then patch the tap formula manually per `docs/homebrew.md`.

## Why a script and not release-please

[release-please](https://github.com/googleapis/release-please) automates the same flow but requires conventional commit messages (`feat:`, `fix:`, etc.) which the project doesn't use today. `bin/release` is intentionally a few hundred lines of bash so the whole flow is auditable in one file, no commit-message convention required, and any step can be done by hand if the script breaks.

If conventional commits become standard practice on this repo, switching to release-please would be straightforward — `bin/release` is small enough to throw away.
