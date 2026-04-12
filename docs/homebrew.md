# Homebrew distribution

Franklin is distributed via a personal Homebrew tap rather than homebrew-core. Homebrew-core requires ~75 stars and demonstrated "notability" before they'll accept a new formula, so a fresh project gets its own tap first and can graduate later.

End-user install:

```bash
brew tap mcrundo/franklin
brew install franklin-book
```

That drops a `franklin` command onto PATH, same as `pipx install franklin-book` would.

---

## How the formula works

The formula at `mcrundo/homebrew-franklin` uses a pip-based install rather than brew's `virtualenv_install_with_resources`. It creates a Python venv, `pip install franklin-book==VERSION` with pre-built wheels from PyPI, and symlinks the `franklin` binary into brew's bin.

This approach was chosen because `virtualenv_install_with_resources` forces `--no-binary :all:` (source builds for every dep), which collides with brew's sandboxed build environment for packages with heavy native extensions (cryptography needs Rust + maturin, pillow needs libjpeg + cmake, lxml needs libxml2). The pip-based install uses binary wheels and completes in ~10 seconds.

The tradeoff: homebrew-core would not accept this formula as-is (they require source builds via `virtualenv_install_with_resources`). That's fine — we're in a personal tap and can migrate if/when the project meets homebrew-core's notability requirements.

---

## Bumping the formula on future releases

**This is automated.** When `bin/release` cuts a new version and pushes the tag, `.github/workflows/homebrew-bump.yml` waits for PyPI to publish the sdist, fetches the new URL + sha256, patches `Formula/franklin-book.rb` in the tap repo, and opens a PR for review. See `docs/releasing.md` for the full release flow.

The auto-PR patches the `url` and `sha256` lines. The `pip install franklin-book==#{version}` line picks up the version from the URL automatically (brew derives `#{version}` from the sdist filename).

### Manual review checklist

The auto-PR is intentionally not auto-merged. Before merging:

1. `brew install franklin-book` to smoke-test.
2. `brew test franklin-book` to verify the test block passes.
3. Merge the PR.

Since the formula uses pip with wheels (not source builds), dependency graph changes don't require any manual intervention — pip resolves them automatically.

### Required secret

The auto-bump workflow needs a `HOMEBREW_TAP_TOKEN` secret in the franklin repo: a fine-grained PAT scoped to `mcrundo/homebrew-franklin` with **Contents: Read and write** and **Pull requests: Read and write**. Configure at https://github.com/settings/tokens?type=beta.

### Manual fallback

If the workflow doesn't fire or fails, the helper script `bin/bump-homebrew` prints the new URL + sha256 for any PyPI release:

```bash
bin/bump-homebrew              # latest PyPI release
bin/bump-homebrew 0.3.0        # specific version
```

The output is ready to paste into the formula's `url` and `sha256` lines.

---

## Why not homebrew-core?

homebrew-core's [acceptable formulae policy](https://docs.brew.sh/Acceptable-Formulae) requires:

- **Notability**: typically 30+ forks, 30+ watchers, or 75+ stars
- **Stable releases**: a tagged version, not HEAD-only
- **Maintained**: commits in the last ~year

Franklin will meet these eventually. Until then, a personal tap is the clean, officially-blessed path and behaves identically from the user's point of view. The migration to homebrew-core later is just `brew bump-formula-pr` against `homebrew/homebrew-core` — but would require switching the formula to `virtualenv_install_with_resources` at that point.
