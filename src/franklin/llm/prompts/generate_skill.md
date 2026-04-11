# Generate the Root SKILL.md

You are generating the top-level `SKILL.md` file for a Claude Code plugin derived from a technical book. This file is the **router** — the front door readers hit when they install the plugin. It should be a navigation hub, not a textbook. Depth belongs in reference files; SKILL.md links out.

## What a SKILL.md looks like

A SKILL.md file has a YAML frontmatter block at the top and then a structured markdown body.

### Frontmatter (required)

```yaml
---
name: <plugin-name>
description: <one or two sentences explaining what the skill is, who it helps, and what triggers it>
allowed-tools:
  - Grep
  - Glob
  - Read
  - Task
---
```

The `description` should clearly say when Claude should use this skill — it's what the tool selector matches against. Include trigger phrases in natural language (e.g. "use when reviewing Rails code for layered architecture violations").

### Body structure

1. `# Plugin Display Name` — heading (not the frontmatter name)
2. **Short intro paragraph** (2–3 sentences) — what the skill does and who it's for
3. **Orientation tables** — this is the main value of the router. Typical tables:
   - A layer or concept map ("where does this code go?") linking to relevant references
   - A pattern catalog table linking to each pattern reference file
   - An anti-pattern quick-reference table with short descriptions and links
   - Common violations or triggers with links to guidance
4. **"What would you like to do?" section** — 4–8 bullet points with user intents, each linking to a command, reference, or agent
5. **Commands reference** — small table of available slash commands
6. **Reference map** — final section listing all reference files by category with short descriptions

Target length: the brief will include an estimated token count (typically 2500–3500). The router should be scannable, not exhaustive.

## Voice and editorial rules (from the plan)

{{coherence_rules}}

## About this book

{{book_context}}

## How to produce a good router

1. **Tables over prose.** The router's job is to answer "which file do I need?" — tables do that faster than paragraphs.
2. **Link to every reference file in the plan.** The reducer will validate that every reference in the plan tree is reachable from SKILL.md.
3. **Use relative markdown links** to reference files (`references/patterns/service-objects.md`). Use plain command names in the commands table (`/spec-test`).
4. **Keep prose short.** Anywhere you're tempted to write a paragraph of explanation, consider whether it belongs in a reference file instead and whether the SKILL.md should just link there.
5. **Match the book's voice** per the coherence rules above.
6. **Frontmatter is required.** Emit the YAML block at the top.

## Full plugin file tree

Every relative markdown link in this file must point to a path that exists in the list below. **Do not invent paths.** Link to every reference file in this tree from SKILL.md — the router's job is to be the single entry point for the whole plugin.

{{plan_tree}}

**Computing relative paths:** the SKILL.md lives at `skills/<plugin>/SKILL.md`. From there, linking to a reference is `references/patterns/X.md` (no leading `../`). Linking to a command is `../../commands/X.md` and linking to an agent is `../../agents/X.md`.

<!-- CACHE-BREAKPOINT -->

## This specific SKILL.md

**Path:** `{{artifact_path}}`

**Brief:** {{artifact_brief}}

**Plugin name (for frontmatter):** `{{plugin_name}}`

**Sidecar slice to work from:**

{{resolved_context}}

---

Call the `save_artifact_file` tool now with the complete SKILL.md contents (including the YAML frontmatter) in the `content` field.
