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
4. **What this agent checks / does** — the concrete responsibilities. For a reviewer, list the rules and anti-patterns it enforces. For a planner/advisor, list the workflows it helps sequence.
5. **Procedure** — an ordered or numbered sequence the agent follows on each invocation. Concrete actions: read files, grep for patterns, identify candidates, report findings with severity, suggest extractions.
6. **Output format** — describe the expected shape of the agent's response (sections, bullets, severity labels, file paths).

Target length: the brief will include an estimated token count (typically 2500–4000). Agents benefit from rich system prompts — take the budget.

## Voice and editorial rules (from the plan)

{{coherence_rules}}

## About this book

{{book_context}}

## How to produce a good agent

1. **Ground the agent in the book.** Its principles, checks, and procedure should draw directly from the sidecar content (rules, anti-patterns, actionable_workflows, decision_rules). Do not invent capabilities.
2. **Be concrete about inputs.** Say what the agent expects the user to provide ("a file path", "a class name", "a diff").
3. **Be concrete about outputs.** Reviewers report findings; planners sequence steps. Describe the exact shape.
4. **Cite references.** When the agent needs to apply a specific rule, point at the relevant reference file (`skills/<plugin>/references/patterns/X.md`).
5. **One agent, one job.** Do not bundle reviewer and planner behavior into one agent — if the plan has both, generate both as separate files.
6. **Frontmatter is required with the plugin-prefixed name.**

<!-- CACHE-BREAKPOINT -->

## This specific agent

**Path:** `{{artifact_path}}`

**Brief:** {{artifact_brief}}

**Sidecar slice (feeds_from content for this agent):**

{{resolved_context}}

---

Call the `save_artifact_file` tool now with the complete agent file contents (including the YAML frontmatter) in the `content` field.
