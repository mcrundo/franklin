# Homebrew distribution

Franklin is distributed via a personal Homebrew tap rather than homebrew-core. Homebrew-core requires ~75 stars and demonstrated "notability" before they'll accept a new formula, so a fresh project gets its own tap first and can graduate later.

End-user install, once everything below is set up:

```bash
brew tap mcrundo/franklin
brew install franklin-book
```

That drops a `franklin` command onto PATH, same as `pipx install franklin-book` would.

---

## One-time tap setup

These steps are done **once**, after `franklin-book` is live on PyPI. They depend on a published PyPI release existing because Homebrew hashes the sdist tarball.

### 1. Create the tap repository on GitHub

Create an empty public repo at `https://github.com/mcrundo/homebrew-franklin`. The `homebrew-` prefix is mandatory — that's how `brew tap mcrundo/franklin` resolves the repo name.

No README required. Homebrew will just look for `Formula/*.rb` files.

### 2. Clone the tap and scaffold

```bash
git clone git@github.com:mcrundo/homebrew-franklin.git
cd homebrew-franklin
mkdir -p Formula
```

### 3. Generate the formula skeleton

Use `brew create --python` against the PyPI sdist URL. Homebrew will download the tarball, compute the sha256, and open an editor with a skeleton:

```bash
brew create --python --tap=mcrundo/homebrew-franklin \
    https://files.pythonhosted.org/packages/source/f/franklin-book/franklin_book-0.1.0.tar.gz
```

In the editor, fill in:

```ruby
class FranklinBook < Formula
  include Language::Python::Virtualenv

  desc "Turn technical books into Claude Code plugins (Opus advises, Sonnet executes)"
  homepage "https://github.com/mcrundo/franklin"
  url "https://files.pythonhosted.org/packages/source/f/franklin-book/franklin_book-0.1.0.tar.gz"
  sha256 "<filled by brew create>"
  license "MIT"

  depends_on "python@3.12"

  # resource blocks get filled in by `brew update-python-resources`
  # (leave this section empty for now)

  def install
    virtualenv_install_with_resources
  end

  test do
    # Smoke test: the binary runs and reports its version
    assert_match "franklin", shell_output("#{bin}/franklin --help")
    # Doctor should exit 0 with --skip-network on a clean box
    system bin/"franklin", "doctor", "--skip-network"
  end
end
```

### 4. Populate the transitive dependency graph

```bash
brew update-python-resources Formula/franklin-book.rb
```

This walks Franklin's declared deps (anthropic, ebooklib, pdfplumber, rich, typer, pydantic, pyjwt, pyyaml, keyring, beautifulsoup4, lxml, tenacity) plus their transitive closure, and writes a `resource "name" do ... end` block for each one. Expect somewhere between 30 and 60 resource blocks.

### 5. Test the formula locally

```bash
# Make sure the tap is loaded
brew tap mcrundo/franklin "$(pwd)"

# Install from source — this exercises the resource graph
brew install --build-from-source franklin-book

# Run the test block
brew test franklin-book

# Audit against Homebrew's style rules
brew audit --strict --new-formula franklin-book
```

Any `brew audit` warnings are worth fixing before committing — they're the same checks homebrew-core would eventually run if we submit for promotion.

### 6. Commit and push

```bash
git add Formula/franklin-book.rb
git commit -m "franklin-book 0.1.0"
git push
```

Users can now run:

```bash
brew tap mcrundo/franklin
brew install franklin-book
```

---

## Bumping the formula on future releases

**This is now automated.** When `bin/release` cuts a new version and pushes the tag, `.github/workflows/homebrew-bump.yml` waits for PyPI to publish the sdist, fetches the new URL + sha256, patches `Formula/franklin-book.rb` in this tap repo, and opens a PR for review. See `docs/releasing.md` for the full release flow.

The auto-PR description includes a checklist for the manual review step:

1. **If dependencies changed**, run `brew update-python-resources Formula/franklin-book.rb` locally to refresh the resource graph. The workflow can't do this on its Linux runner — it needs Homebrew installed.
2. `brew install --build-from-source franklin-book` to smoke-test.
3. `brew test franklin-book` and `brew audit --strict --new-formula franklin-book`.
4. Merge the PR.

### Required secret

The auto-bump workflow needs a `HOMEBREW_TAP_TOKEN` secret in the franklin repo: a fine-grained PAT scoped to `mcrundo/homebrew-franklin` with **Contents: Read and write** and **Pull requests: Read and write**. Configure at https://github.com/settings/tokens?type=beta.

### Manual fallback

If the workflow doesn't fire or fails for some reason, the helper script `bin/bump-homebrew` (in the main franklin repo) prints the new URL + sha256 for any PyPI release so you can patch the formula by hand:

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

Franklin will meet these eventually. Until then, a personal tap is the clean, officially-blessed path and behaves identically from the user's point of view. The migration to homebrew-core later is just `brew bump-formula-pr` against `homebrew/homebrew-core`.
