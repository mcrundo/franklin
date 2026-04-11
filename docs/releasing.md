# Releasing Franklin

Releases are mostly automated. The only manual command is one shell script.

## TL;DR

```bash
bin/release 0.3.0
```

That's it. The script handles bump, changelog cut, sanity gate, commit, tag, and push (with a confirmation prompt). After the push, three GitHub workflows fire in sequence and produce the GitHub Release, the PyPI upload, and a Homebrew tap PR.

## What `bin/release` does

1. **Preflight.** Verifies semver format, clean working tree, you're on `main`, `main` is in sync with `origin/main`, the `[Unreleased]` CHANGELOG section has actual content, the version is different from the current one, and no `vX.Y.Z` tag exists locally or on origin.
2. **Version bump.** Edits `version` in `pyproject.toml`, `__version__` in `src/franklin/__init__.py`, and runs `uv lock` to refresh the lockfile.
3. **Changelog cut.** Inserts a `## [X.Y.Z] - YYYY-MM-DD` heading right below `## [Unreleased]`, leaving the existing entries to fall under the new dated header. Updates the link references at the bottom of the file (`[Unreleased]` and adds `[X.Y.Z]`).
4. **Sanity gate.** Runs `ruff check`, `ruff format --check`, `mypy`, and `pytest -q`. Fails fast if any check is red.
5. **Commit + tag.** Single `Release X.Y.Z` commit, tagged `vX.Y.Z`.
6. **Push prompt.** Asks before pushing. Pass `--yes` to skip the prompt for scripted use, or `--dry` to do everything except commit/tag/push (useful for inspecting the diff).

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

You then review and merge the tap PR. Manual review is intentional — if dependencies changed, you'll want to re-run `brew update-python-resources Formula/franklin-book.rb` locally before merging, since that needs Homebrew installed and the workflow can't do it on a Linux runner.

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
