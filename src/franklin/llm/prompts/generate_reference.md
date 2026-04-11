# Generate a Reference File

You are generating one reference file for a Claude Code plugin derived from a technical book. The file lives under `skills/<plugin>/references/` and is linked from the main SKILL.md router. Readers reach it when they need depth on one specific topic or pattern.

## What a reference file looks like

Reference files are plain markdown — **no frontmatter block**. They are read top-to-bottom by a developer who needs detail, not skimmed for navigation. Follow this structure:

1. `# Title` — one-line heading naming the topic
2. **Overview** — one short paragraph (2–3 sentences) saying what this topic is and why it matters in the context of the book
3. **Main sections** — the depth content, organized by subtopic. Use `##` headings freely.
4. **When to use / When NOT to use** — for pattern references, a short section with clear criteria drawn from the book's decision rules
5. **Code examples** — in fenced blocks with language identifiers (```ruby, ```yaml, ```erb), quoted verbatim from the sidecar data
6. **Related references** — closing section with relative markdown links to related files in the plugin tree

Target length: the brief will include an estimated token count (typically 2000–4000). Write tightly. Don't pad.

## Voice and editorial rules (from the plan)

{{coherence_rules}}

## About this book

{{book_context}}

## How to produce faithful content

1. **Quote the book.** Use concepts, definitions, rules, and code examples from the sidecar data verbatim or near-verbatim. Do not invent examples or fabricate API details.
2. **Preserve code exactly.** Code blocks must be copied as-is including whitespace. Use the author's own labels ("Listing 3.1") when the sidecar records one.
3. **Cite sparingly.** An italicized `_source: chNN §X_` at the end of a non-obvious claim is enough. Do not cite every sentence.
4. **Link, don't duplicate.** Cross-reference other files with relative markdown links when the topic overlaps. See the plugin file tree below for the only valid link targets.
5. **Start with the problem.** Follow the book's teaching pattern — show the Rails-native approach first, name the trigger that prompts extraction, then present the pattern.
6. **No frontmatter.** Reference files are plain markdown only.

## Full plugin file tree

Every relative markdown link in this file must point to a path that exists in the list below. **Do not invent paths or link to files not listed here.** If the concept you want to link to doesn't have a reference file in this plugin, don't link at all — describe the concept inline instead.

{{plan_tree}}

**Computing relative paths:** the file you're generating will live at the path shown in the "This specific reference" section below. Compute every relative link from that starting directory. Reference files sit **four levels deep** inside the plugin root (`skills/<plugin>/references/<category>/X.md`), so linking out of the references tree requires four `..` segments. From `skills/<plugin>/references/patterns/X.md`:

- linking to a file in the same directory: `other-file.md`
- linking to a sibling references subdirectory (core, topics, anti-patterns, examples): `../core/other.md`
- linking to a command: `../../../../commands/X.md` (four levels up to the plugin root, then into `commands/`)
- linking to an agent: `../../../../agents/X.md` (same depth as commands)
- linking to the root SKILL.md: `../../SKILL.md`

Count your `..` segments carefully. An off-by-one in the depth will produce a broken link.

<!-- CACHE-BREAKPOINT -->

## This specific reference

**Path:** `{{artifact_path}}`

**Brief:** {{artifact_brief}}

**Sidecar slice to work from (the `feeds_from` content for this artifact):**

{{resolved_context}}

---

Call the `save_artifact_file` tool now with the complete file contents in the `content` field. Do not reply with prose outside the tool call.
