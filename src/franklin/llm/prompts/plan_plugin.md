# Design a Claude Code Plugin from a Distilled Book

You are designing a Claude Code plugin from a technical book. The book has already been read and distilled into structured per-chapter extractions (provided below in full). Your output will become the architecture for a plugin that developers install and rely on daily — so shape matters as much as coverage. The best plugins mirror the structure of the book's content: a principles-heavy book becomes a different shape than a patterns-heavy book, and you should let that decide the plan.

## What a Claude Code Plugin Looks Like

A plugin is a directory tree that developers install via the marketplace or as a local path. The shape:

```
plugin-name/
├── .claude-plugin/plugin.json       (metadata only)
├── skills/<skill-name>/SKILL.md     (the router — exactly one per plugin)
├── skills/<skill-name>/references/  (markdown reference files, organized into subdirectories)
├── commands/<name>.md               (slash commands)
└── agents/<name>.md                 (subagents)
```

## Artifact Types You Can Propose

- **skill** — Exactly one per plugin. The top-level `SKILL.md` that routes readers to everything else. It should read like a navigation hub: a short introduction, a few orienting tables ("where does this code go", "pattern catalog", "common violations"), and a pattern of links out to reference files. **Keep it a router, not a textbook.** Depth belongs in reference files.

- **reference** — A markdown file under `skills/<skill>/references/`. Organize in subdirectories by category. Standard categories are `core/` (foundational principles and architecture), `patterns/` (named design patterns), `topics/` (subject-specific deep dives like authorization or testing), and `examples/` (worked end-to-end examples). **You are free to invent additional category directories** when the book's material genuinely calls for one — a book that covers specific gems can have a `gems/` directory, a book with a rich anti-patterns chapter can have a standalone `anti-patterns.md` or an `anti-patterns/` directory, and so on. The goal is a tree that mirrors the book's own mental model.

- **command** — A slash command under `commands/<name>.md`. Propose one when the book describes a **concrete, repeatable workflow** that a reader would naturally want to invoke by name. Good candidates come directly from `actionable_workflows` entries in the sidecars. Bad candidates are vague "review my code" commands without a specific procedure.

- **agent** — A subagent under `agents/<name>.md`. Propose one when the book supports a **delegation pattern**. Typical cases: a *reviewer* agent that applies the book's rules and anti-patterns to code the user provides; a *planner* agent that helps sequence a gradual adoption or refactoring. Only propose an agent if the book has enough material (rules, anti-patterns, workflows) to make its responses substantive.

## Your Task

Read the distilled book below in full, then call the `save_plan_proposal` tool with a complete plan:

1. **plugin** — Name, version (`0.1.0`), short description, and keywords. Use lowercase-kebab-case for the name.
2. **planner_rationale** — 2–4 sentences explaining your high-level design decision. What kind of plugin is this (reference-heavy, workflow-heavy, review-focused)? What in the book led you there? Which artifact counts reflect the content weight?
3. **artifacts** — Every file to generate, each with:
   - `id` — short stable identifier like `art.skill.root`, `art.ref.service-objects`, `art.agent.reviewer`, `art.cmd.review`
   - `type` — one of `skill`, `reference`, `command`, `agent`
   - `path` — the relative path inside the plugin tree (e.g. `skills/layered-rails/references/patterns/service-objects.md`)
   - `brief` — one or two sentences describing what the file should contain
   - `feeds_from` — list of dotted paths into the sidecars the reduce stage will use. Valid forms: `chNN.concepts`, `chNN.anti_patterns`, `chNN.actionable_workflows`, etc. You can also cite whole sidecars (`chNN`) or book-level data (`book.metadata`, `book.cross_chapter_themes`).
   - `estimated_output_tokens` — a rough estimate (1500–4000 for a reference file, 500–2000 for a command, 2000–4000 for an agent).
4. **coherence_rules** — 3–8 short instructions for the reduce stage to keep output consistent (terminology choices, citation style, linking conventions, voice).
5. **skipped_artifact_types** — Artifact types you chose NOT to propose, with reasons. Valid entries include `mcp_server`, `hook`, `output_style`, `statusline`. Be explicit — if the book has no API content, say so.
6. **estimated_total_output_tokens** and **estimated_reduce_calls** — rough totals across all artifacts.

## Rules

1. **Exactly one skill.** Every plugin has exactly one `SKILL.md` router.
2. **Only propose artifacts the sidecars support.** If the book has no anti-patterns, don't propose a reviewer agent. If there are no actionable workflows, don't propose commands. Empty or generic proposals are worse than skipping.
3. **Every `feeds_from` must reference real sidecar data.** Do not invent chapter IDs or categories.
4. **Keep the skill file a router, not a textbook.** The SKILL.md should be small (2000–3500 output tokens) and mostly tables and links.
5. **Bias toward fewer, higher-quality artifacts.** 15 well-designed references beat 40 bloated ones. Combining related concepts into one reference file is often the right call.
6. **Invent category directories freely** when the book's material genuinely calls for one, but do not invent categories to pad the output.
7. **Use lowercase-kebab-case** for file names, directory names, and artifact IDs.

## Distilled Book

{{distilled_book}}

---

Call `save_plan_proposal` now with your full plan. Do not respond with prose.
