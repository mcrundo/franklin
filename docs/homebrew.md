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

Every new Franklin release needs a formula update:

1. Download new sdist URL from PyPI, update `url` and `sha256` in the formula.
2. Re-run `brew update-python-resources Formula/franklin-book.rb` to catch new or updated transitive deps.
3. `brew audit --strict --new-formula franklin-book` to check for drift.
4. Commit and push to the tap repo.

The helper script `bin/bump-homebrew` (in the main franklin repo) prints the new URL + sha256 for the latest PyPI release so you don't have to look them up manually. See its `--help`.

### Future: automate it end-to-end

A GitHub Action on the main franklin repo can watch for `release:published` events, clone the tap, run the bump steps, and open a PR. That's worth building once manual bumps start feeling repetitive. Not blocking for v0.1.0.

---

## Why not homebrew-core?

homebrew-core's [acceptable formulae policy](https://docs.brew.sh/Acceptable-Formulae) requires:

- **Notability**: typically 30+ forks, 30+ watchers, or 75+ stars
- **Stable releases**: a tagged version, not HEAD-only
- **Maintained**: commits in the last ~year

Franklin will meet these eventually. Until then, a personal tap is the clean, officially-blessed path and behaves identically from the user's point of view. The migration to homebrew-core later is just `brew bump-formula-pr` against `homebrew/homebrew-core`.
