# Generate a Slash Command

You are generating one slash command file for a Claude Code plugin derived from a technical book. The file lives under `commands/<name>.md` and is invoked by a user typing `/<name>` in Claude Code. The command should execute a concrete, repeatable workflow drawn directly from the book's `actionable_workflows`.

## What a command file looks like

A command file has a YAML frontmatter block at the top and a structured markdown body.

### Frontmatter (required)

```yaml
---
description: <one short sentence describing what the command does>
argument-hint: <optional usage hint shown in the command palette, e.g. "[class-name or file-path]">
---
```

Keep the description short — it's what appears in the command palette when the user types `/`.

### Body structure

The body is the instructions Claude follows when the user invokes the command. Use this template:

1. `# Command Name` — heading
2. **Purpose** — 1–2 sentences: what this command does and when to use it
3. **When to use** — a short bullet list of trigger conditions drawn from the book
4. **Steps** — an ordered list Claude will execute. Each step should be concrete and actionable (use tools, read files, ask clarifying questions). Draw the sequence directly from the book's workflow.
5. **Verify** — a short section describing how the user (or Claude) confirms the command succeeded. This is the coherence-rule requirement: every command must have a verification story.
6. **Notes** — optional brief notes about edge cases or related patterns, with links to relevant reference files

Target length: the brief will include an estimated token count (typically 1000–2000). Commands are action scripts, not reference material — keep them tight.

## Voice and editorial rules (from the plan)

{{coherence_rules}}

## About this book

{{book_context}}

## How to produce a good command

1. **Ground steps in the book's workflow.** Use the `actionable_workflows` entries from the sidecar slice as the spine. Do not invent steps.
2. **Speak to Claude, not to the user.** The body is instructions Claude executes. Say "Read the target file", "Grep for X", "Use the Task tool to delegate Y" — not "You should think about".
3. **Name specific tools** where applicable (Read, Grep, Glob, Edit, Task). The frontmatter doesn't need to list them — Claude has the default toolset.
4. **Include a verify step.** Every command must end with a clear way to tell whether the extraction, refactor, or analysis succeeded.
5. **Link to reference files** for deeper pattern explanations. See the plugin file tree below for the only valid link targets.
6. **Frontmatter is required.**

## Full plugin file tree

Every relative markdown link in this file must point to a path that exists in the list below. **Do not invent paths or link to files not listed here.** If the concept you want to link to doesn't have a reference file in this plugin, don't link at all — describe the concept inline instead.

{{plan_tree}}

**Computing relative paths:** the file you're generating will live at the path shown in the "This specific command" section below (typically `commands/X.md`). From there, linking to a reference means traversing up and then into skills: `../skills/<plugin>/references/patterns/X.md`. Linking to another command in the same directory is just `X.md`.

<!-- CACHE-BREAKPOINT -->

## This specific command

**Path:** `{{artifact_path}}`

**Brief:** {{artifact_brief}}

**Sidecar slice (feeds_from content for this command):**

{{resolved_context}}

---

Call the `save_artifact_file` tool now with the complete command file contents (including the YAML frontmatter) in the `content` field.
