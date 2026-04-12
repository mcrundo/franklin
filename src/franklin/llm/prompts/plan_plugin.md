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

- **command** — A slash command under `commands/<name>.md`. Propose one when the book describes a **concrete, repeatable workflow** that a reader would naturally want to invoke by name. Good candidates come directly from `actionable_workflows` entries in the sidecars — look for workflows with a clear trigger condition and an ordered sequence of steps.

  Commands are cheap to add and high-leverage: a developer with a command for a common task has a smoother experience than one who has to re-explain the procedure every time. **When the sidecars contain many actionable_workflows (more than 20 across the whole book), that is a strong signal the book is workflow-heavy and commands should be harvested generously rather than curated sparingly.** As a rough calibration, a book with around 40 workflows should produce on the order of 5–8 commands, not 1 or 2. Under-harvesting commands is the more common failure mode than over-harvesting them.

  Bad candidates are vague commands without a specific procedure ("review my code"), or commands that exactly duplicate what a subagent already handles better.

- **agent** — A subagent under `agents/<name>.md`. Propose one when the book supports a **delegation pattern** — a task the reader wants to hand off to an isolated context rather than ask inline. The two common shapes:

  - **Reviewer** — applies the book's rules and anti-patterns to code the user provides. Powered primarily by `anti_patterns`, `rules`, and `decision_rules` in the sidecars. Answers the question *"what's wrong with this code?"*
  - **Planner / Advisor** — sequences a multi-step adoption, refactoring, or migration. Powered primarily by `actionable_workflows` in the sidecars. Answers the question *"given this codebase and where I want to end up, what do I do next?"*

  These are genuinely distinct use cases. A reviewer tells you what's wrong; a planner tells you what to do about it. **If the book supports both — a rich anti-pattern catalog AND many actionable workflows — propose them as two separate agents, not one combined agent.** Bundling planner behavior into a reviewer loses the distinction and leaves the planning use case unserved.

  Books that teach **gradual adoption, progressive extraction, or staged migration** as their methodology are particularly strong candidates for a dedicated planner agent, because the book's whole thesis *is* a planning task. If the sidecars show the book emphasizing "don't start with abstractions, extract as you grow" or equivalent gradual-adoption language, that is a direct signal to propose a planner agent alongside any reviewer.

  Only propose an agent when the sidecars provide substantive material; empty or boilerplate agents are worse than none.

## Your Task

Read the distilled book below in full, then call the `save_plan_proposal` tool with a complete plan:

1. **plugin** — Name, version (`0.1.0`), short description, and keywords. Use lowercase-kebab-case for the name.
2. **planner_rationale** — 2–4 sentences explaining your high-level design decision. What kind of plugin is this (reference-heavy, workflow-heavy, review-focused)? What in the book led you there? Which artifact counts reflect the content weight?
3. **artifacts** — Every file to generate, each with:
   - `id` — short stable identifier like `art.skill.root`, `art.ref.service-objects`, `art.agent.reviewer`, `art.cmd.review`
   - `type` — one of `skill`, `reference`, `command`, `agent`
   - `path` — the relative path inside the plugin tree (e.g. `skills/layered-rails/references/patterns/service-objects.md`)
   - `brief` — one or two sentences describing what the file should contain. For commands, start with an action verb ("Extract business logic from...", "Create a migration following...") — do NOT prefix with "Slash command to" or similar boilerplate.
   - `feeds_from` — list of **category-level** dotted paths into the sidecars. Valid categories: `concepts`, `principles`, `rules`, `anti_patterns`, `code_examples`, `decision_rules`, `actionable_workflows`, `terminology`, `cross_references`. Valid forms: `chNN` (whole sidecar), `chNN.concepts`, `chNN.anti_patterns`, `chNN.actionable_workflows`, `book.metadata`, `book.cross_chapter_themes`. **NEVER use individual item IDs** like `ch04.workflow.create-new-app` — only the category name after the dot (e.g. `ch04.actionable_workflows`).
   - `estimated_output_tokens` — a rough estimate (1500–4000 for a reference file, 500–2000 for a command, 2000–4000 for an agent).
4. **coherence_rules** — 3–8 short instructions for the reduce stage to keep output consistent (terminology choices, citation style, linking conventions, voice).
5. **skipped_artifact_types** — Artifact types you chose NOT to propose, with reasons. Valid entries include `mcp_server`, `hook`, `output_style`, `statusline`. Be explicit — if the book has no API content, say so.
6. **estimated_total_output_tokens** and **estimated_reduce_calls** — rough totals across all artifacts.

## Rules

1. **Exactly one skill.** Every plugin has exactly one `SKILL.md` router.
2. **Only propose artifacts the sidecars support.** If the book has no anti-patterns, don't propose a reviewer agent. If there are no actionable workflows, don't propose commands. Empty or generic proposals are worse than skipping.
3. **Every `feeds_from` must reference real sidecar data.** Do not invent chapter IDs or categories.
4. **Keep the skill file a router, not a textbook.** The SKILL.md should be small (2000–3500 output tokens) and mostly tables and links.
5. **Bias toward fewer, higher-quality reference files.** 15 well-designed references beat 40 bloated ones. Combining related concepts into one reference file is often the right call. *This bias applies to reference files only — it does NOT apply to commands, which should be harvested generously from actionable workflows, or to agents, which should be split by use case when the material supports both a reviewer and a planner.*
6. **Invent category directories freely** when the book's material genuinely calls for one, but do not invent categories to pad the output.
7. **Use lowercase-kebab-case** for file names, directory names, and artifact IDs.
8. **Name commands as verb-noun pairs** (`extract-service`, `create-migration`, `write-system-test`). Generic names (`review`, `analyze`) are too vague — they should describe the specific action the command performs.
9. **The SKILL.md must include an anti-pattern quick reference** if the book contains anti-patterns. A small table with columns `| Anti-pattern | Why harmful | Reference |` gives users an instant overview of what NOT to do. This is one of the highest-value sections in a plugin.

## Distilled Book

{{distilled_book}}

---

Call `save_plan_proposal` now with your full plan. Do not respond with prose.
