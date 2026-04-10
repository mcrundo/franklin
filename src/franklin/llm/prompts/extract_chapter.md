# Extract Structured Knowledge from a Book Chapter

You are extracting structured knowledge from one chapter of a technical book. Your output will be compiled into a Claude Code plugin that future developers will rely on as a faithful reference to this book. Precision and fidelity to the author's voice matter more than coverage.

## Book Context

- **Title:** {{book_title}}
- **Authors:** {{book_authors}}
- **This chapter:** {{chapter_title}} ({{chapter_id}})
- **Word count:** {{word_count}}

## How to Extract

You must respond by calling the `save_chapter_extraction` tool exactly once. Do not reply with prose. Populate each category with the items that genuinely appear in the chapter — empty lists are expected and correct when a category is not represented.

### Categories

- **concepts** — Key terms and ideas the chapter introduces or defines. Things a reader should walk away knowing. Mark `importance: high` for concepts the chapter is *about* (usually named in the title or opening paragraphs); everything else is `medium` or `low`.
- **principles** — General beliefs, maxims, or rules the author advocates across the chapter. Framed as statements ("X should Y"). These are the author's *opinions* and design philosophy.
- **rules** — Specific, actionable directives. Stricter than principles. A rule should be checkable: given a piece of code, can you tell whether the rule holds? Record exceptions the author notes explicitly.
- **anti_patterns** — Named mistakes, smells, or failure modes the author warns against. Each needs a `fix` describing how to move away from the anti-pattern. If the chapter shows before/after code, reference the code example IDs in `code_before_ref` and `code_after_ref`.
- **code_examples** — Code snippets worth preserving verbatim as reference. Do NOT paraphrase or shorten. Use the author's own labels (e.g. "Listing 3.1") when present. Include a short `context` sentence explaining what the snippet illustrates.
- **decision_rules** — "When should I use X?" guidance the author provides. Populate `yes_when` and `no_when` with the conditions the author lists.
- **actionable_workflows** — Ordered step-by-step procedures the reader can follow (e.g. "how to extract a service from a fat model"). These become slash commands downstream, so they must be concrete and sequential.
- **terminology** — Glossary-style term definitions specific to this book's vocabulary. Only include terms the author explicitly defines or uses in a non-standard way.
- **cross_references** — Mentions of other chapters this chapter depends on or relates to. Use the form `to_chapter: "chNN"` if you can infer the chapter number, otherwise describe it in `reason`.

### ID and source_location requirements

Every item in every category must have:

1. A **stable `id`** of the form `{{chapter_id}}.<category_short>.<slug>` where `<category_short>` is one of `concept`, `principle`, `rule`, `anti`, `example`, `decision`, `workflow`, `term`, and `<slug>` is a short kebab-case identifier. Examples: `{{chapter_id}}.concept.service-object`, `{{chapter_id}}.anti.fat-model`, `{{chapter_id}}.example.good-service`. IDs must be unique within this chapter.

2. A **`source_location`** pointing back into the chapter. Use the form `{{chapter_id}} §N.M` for section references when the chapter has numbered sections, or `{{chapter_id}} listing N.M` for code listings with numeric labels, or a short descriptive phrase like `{{chapter_id}} opening paragraphs` when no clearer anchor exists. Be consistent within the chapter.

### Rules of faithfulness

1. **Do not invent content.** If the chapter has no anti-patterns, return `anti_patterns: []`. Blank categories are expected.
2. **Preserve the author's voice** for definitions and rules. Paraphrase only when needed for brevity, and preserve technical terms exactly as the author writes them.
3. **Code must be verbatim.** Copy snippets exactly as they appear. Do not reformat, simplify, or add comments.
4. **Cite precisely.** A vague source_location is worse than a descriptive one.
5. **Bias toward fewer, higher-quality items.** A chapter with five well-chosen concepts is more useful than one with twenty diluted ones.
6. **The summary is one or two sentences** capturing what this chapter is about and why it matters in the context of the whole book.

## Chapter Prose

{{chapter_text}}

## Code Blocks From This Chapter

The following code blocks were extracted from the chapter in the order they appeared. Reference them in `code_examples` verbatim, and in `anti_patterns` via `code_before_ref` / `code_after_ref` when the author contrasts bad/good code.

{{code_blocks}}

---

Call `save_chapter_extraction` now with your structured extraction.
