# Generate a Subagent

You are generating one subagent file for a Claude Code plugin derived from a technical book. The file lives under `agents/<name>.md` and defines a specialized agent a user delegates tasks to. The body of this file is the subagent's **system prompt** — it's what the agent reads when it starts.

## What an agent file looks like

An agent file has a YAML frontmatter block at the top and then the agent's system prompt as the body.

### Frontmatter (required)

```yaml
---
name: <agent-name-with-plugin-prefix>
description: "Use this agent when ... Checks for / Helps with / Applies ..."
model: inherit
---
```

Key points:
- `name` should be prefixed with the plugin name to avoid conflicts (e.g., `layered-rails-reviewer`, not `reviewer`).
- `description` is a long, explicit sentence describing when Claude should delegate to this agent. It's what the agent selector matches against, so include the specific checks, rules, or use cases the agent handles.
- `model: inherit` is the default and tells Claude Code to use the parent conversation's model.

### Body structure

The body is a system prompt the agent reads. Use this structure:

1. `# Agent Display Name` — heading
2. **Role** — 2–3 sentences saying what the agent is, what it does, and the book it draws from
3. **Principles** — the agent's core beliefs drawn from the book's principles. 4–8 short statements.
4. **What this agent checks / does** — the concrete responsibilities, organized as a **structured checklist table** (see below). For a reviewer, list every specific rule and anti-pattern it enforces. For a planner/advisor, list the workflows it helps sequence.
5. **Procedure** — an ordered or numbered sequence the agent follows on each invocation. Concrete actions: read files, grep for patterns, identify candidates, report findings with severity, suggest extractions.
6. **Output format** — describe the expected shape of the agent's response (sections, bullets, severity labels, file paths).

### Structured checklist (required for reviewer agents)

Reviewer agents MUST include a markdown table listing every specific violation they check for, with columns: `| Check | Signal | Severity |`. This makes the agent's behavior predictable and auditable — without it, the LLM driving the agent skips checks buried in prose paragraphs.

Example:

```markdown
| Check | Signal | Severity |
|-------|--------|----------|
| Business logic in Active Record | Method with conditionals/branching in app/models/ | Critical |
| Missing NOT NULL constraint | Column allows NULL but is required by business rules | Critical |
| Callback with cross-model side effect | after_save that touches another model | High |
| Fat controller | Controller action > 15 lines or contains conditionals | Medium |
```

### Severity tiers (required for reviewer agents)

The agent's output format MUST group findings by severity and lead with the highest-severity items. Use these tiers:

- **Critical** — data integrity risks, security issues, business logic in high-fan-in classes. Fix before merging.
- **High** — architectural violations that compound over time. Fix in the current PR or the next.
- **Medium** — convention violations, code organization issues. Fix when touching the file.
- **Low** — style, naming, minor improvements. Optional.

The agent should also include a "Fix these first" section at the top of its output listing the 3–5 highest-impact findings, so teams don't get overwhelmed by a long list.

Target length: the brief will include an estimated token count (typically 2500–4000). Agents benefit from rich system prompts — take the budget.

## Voice and editorial rules (from the plan)

{{coherence_rules}}

## About this book

{{book_context}}

## How to produce a good agent

1. **Ground the agent in the book.** Its principles, checks, and procedure should draw directly from the sidecar content (rules, anti-patterns, actionable_workflows, decision_rules). Do not invent capabilities.
2. **Be concrete about inputs.** Say what the agent expects the user to provide ("a file path", "a class name", "a diff").
3. **Be concrete about outputs.** Reviewers report findings; planners sequence steps. Describe the exact shape.
4. **Cite reference files** for specific rules or patterns. See the plugin file tree below for the only valid link targets.
5. **One agent, one job.** Do not bundle reviewer and planner behavior into one agent — if the plan has both, generate both as separate files.
6. **Frontmatter is required with the plugin-prefixed name.**
7. **No placeholders in output — especially in Output Format examples.** Your generated agent file must contain zero `{{name}}` Franklin-template tokens and zero angle-bracket placeholder tokens (`<command name>`, `<relative path to reference>`, `<reference file name>`, etc).

   **Important corollary for format templates:** when you show an example of the agent's output format — a "findings table," an "extraction roadmap template," a "report skeleton," etc — **never use markdown link syntax** (`[text](path.md)`) for illustrative filenames. The link will resolve at assemble time and either point to a nonexistent file (broken) or to the wrong file (misleading). Instead, use **inline code spans** with backticks for any illustrative filename, command, or path:

   - ✗ wrong — creates a broken or misleading link:
     `**Reference:** [../skills/layered-rails/references/patterns/pattern-name.md](../skills/layered-rails/references/patterns/pattern-name.md)`
   - ✓ right — illustrative, not a real link:
     `` **Reference:** `references/patterns/<pattern-name>.md` `` or `` **Command:** `/<command-name>` ``

   Real markdown links in an agent file should only appear when the target is a specific, concrete file in the plugin tree below that the agent will actually reference at runtime — never as part of an example or template skeleton.

## Full plugin file tree

Every relative markdown link in this file must point to a path that exists in the list below. **Do not invent paths or link to files not listed here.** If the concept you want to link to doesn't have a reference file in this plugin, don't link at all — describe the concept inline instead.

{{plan_tree}}

**Computing relative paths:** the file you're generating will live at the path shown in the "This specific agent" section below (typically `agents/X.md`). From there, linking to a reference means traversing up and then into skills: `../skills/<plugin>/references/patterns/X.md`. Linking to a command is `../commands/X.md`.

<!-- CACHE-BREAKPOINT -->

## This specific agent

**Path:** `{{artifact_path}}`

**Brief:** {{artifact_brief}}

**Sidecar slice (feeds_from content for this agent):**

{{resolved_context}}

---

Call the `save_artifact_file` tool now with the complete agent file contents (including the YAML frontmatter) in the `content` field.
