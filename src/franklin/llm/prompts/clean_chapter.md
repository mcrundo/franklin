# Clean PDF-Extracted Prose

You are cleaning up prose extracted from a PDF by franklin's heuristic layout-aware extractor. The extractor did its best but has known artifacts you need to fix. Your job is **mechanical cleanup only** — you are not rewriting, paraphrasing, or improving the author's prose.

## Artifacts to fix

1. **Word concatenations** — adjacent words sometimes merged without spaces because the PDF's inter-word spacing fell below the extractor's detection threshold. Examples from real runs:
   - `"ButthereisonepartofeveryappthatRailsdoesnthaveaclearanswerfor"` → `"But there is one part of every app that Rails doesn't have a clear answer for"`
   - `"veeverexperiencedawebsiteorappgiveyouageneralsenseofconfidence"` → `"ve ever experienced a website or app give you a general sense of confidence"` (the `"ve"` at the start is a fragment from a sentence break like `"Have you ever..."` — preserve it as a fragment, don't invent text)
   - Use your judgment to split at real English word boundaries.

2. **Hyphen-broken words** — words split across a line break with a hyphen. The extractor concatenates them without always removing the hyphen:
   - `"Incon-solata"` → `"Inconsolata"`
   - `"con-figuration"` → `"configuration"`

3. **Stray footnote markers** — superscript digits in the original PDF sometimes appear as inline numbers that break sentences:
   - `"the answer was yes 3 which we explored"` → `"the answer was yes which we explored"` (remove the stray "3")
   - **But** leave digits that are part of the content alone: `"Rails 5 introduced"`, `"12-factor app"`, `"SECRET_KEY_BASE"`.
   - When unsure, leave the digit in place.

4. **Leftover page furniture** — occasional header/footer text that survived the extractor's y-coordinate filter and got dropped into the middle of a sentence:
   - A lone `"42"` or `"Chapter 5"` on its own line in the middle of a paragraph is furniture — remove it.
   - Running heads like `"Sustainable Rails"` repeated every few pages are furniture — remove them.

5. **Line-break joining** — if a single sentence was split across lines by the extractor's line-clustering, rejoin it into one sentence. Preserve paragraph breaks (usually signaled by larger y-gaps that showed up as blank lines or heading text between paragraphs).

## Strict rules

1. **Do NOT paraphrase, rewrite, shorten, or improve the author's prose.** Fix only the mechanical artifacts above.
2. **Do NOT add content.** If a sentence fragment is genuinely incomplete, leave it incomplete rather than inventing text to fill it in.
3. **Do NOT rewrite inline code mentions.** Tokens like `ActiveBusinessLogic::Base`, `bin/rails`, `app/services`, `Rails.configuration`, `:s3` — preserve exactly as they appear. They are not English and should not be "corrected."
4. **Preserve paragraph breaks.** When the original has a clear paragraph separation (usually a blank-ish line in the raw input), keep it in the output.
5. **Preserve the author's voice and vocabulary.** Technical books have specific tone, terminology, and opinionated phrasing — do not smooth them out.
6. **Do not add section headings that aren't in the source.** If a heading is already there, keep it; don't invent new ones.
7. **Call `save_cleaned_chapter` exactly once with the full cleaned text in the `cleaned_text` field.** Do not reply with prose outside the tool call.

## Chapter to clean

- **Title:** {{chapter_title}}
- **Chapter ID:** {{chapter_id}}
- **Word count before cleanup:** {{word_count}}

### Raw extracted text

{{chapter_text}}

---

Call `save_cleaned_chapter` now with the full cleaned prose. Return the entire chapter, not a diff or a summary.
