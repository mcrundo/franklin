"""Pure-Python run grader that produces a grade card at the end of assemble.

Everything here reads from disk only — no LLM calls, no network. The grader
loads the run's ``plan.json``, walks the assembled plugin tree, runs the
three structural validators (links, template leaks, frontmatter), and
applies a per-artifact structural rubric keyed on the artifact type.

The composite grade is a blend of four signals:

1. **Stage-success gate** — the caller marks any failed stage; any failure
   drops the composite to an F regardless of structural scores.
2. **Validator score** — fraction of markdown files with no broken links,
   no template leaks, and no frontmatter issues.
3. **Coverage score** — fraction of the plan's artifacts whose ``feeds_from``
   list is non-empty. The planner is supposed to wire every artifact to at
   least one sidecar slice; empty lists mean the plan is anemic.
4. **Structural score** — average of per-artifact rubric fractions. Each
   artifact type has a small list of regex/structural checks; the artifact's
   score is ``passed_checks / total_checks``.

The grade is advisory, not a gate. ``franklin assemble`` still exits zero
even on a B-grade run — the user decides whether to push or regenerate.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from franklin.assembler.frontmatter import FrontmatterIssue, validate_frontmatter
from franklin.assembler.links import BrokenLink, validate_links
from franklin.assembler.templates import TemplateLeak, find_template_leaks
from franklin.checkpoint import RunDirectory
from franklin.schema import Artifact, ArtifactType, PlanManifest

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RubricCheck:
    """One named structural check against an artifact file."""

    name: str
    passed: bool


@dataclass(frozen=True)
class ArtifactGrade:
    artifact_id: str
    artifact_type: str
    path: str
    score: float  # 0.0-1.0
    letter: str
    checks: list[RubricCheck]

    @property
    def failed_checks(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


@dataclass(frozen=True)
class ValidatorTotals:
    broken_links: int
    template_leaks: int
    frontmatter_issues: int
    markdown_files: int

    @property
    def total_issues(self) -> int:
        return self.broken_links + self.template_leaks + self.frontmatter_issues


@dataclass(frozen=True)
class RunGrade:
    """The complete grade card for one run."""

    run_dir: str
    plugin_name: str
    graded_at: datetime
    composite_score: float  # 0.0-1.0
    letter: str
    validator_totals: ValidatorTotals
    coverage_fraction: float
    structural_average: float
    artifact_grades: list[ArtifactGrade]
    failed_stages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def lowest_graded(self) -> list[ArtifactGrade]:
        """Up to three lowest-scoring artifacts, ascending by score."""
        return sorted(self.artifact_grades, key=lambda g: g.score)[:3]

    def to_metrics_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["graded_at"] = self.graded_at.isoformat()
        return data


# ---------------------------------------------------------------------------
# Letter grade mapping
# ---------------------------------------------------------------------------


_LETTER_THRESHOLDS: list[tuple[float, str]] = [
    (0.93, "A"),
    (0.90, "A-"),
    (0.87, "B+"),
    (0.83, "B"),
    (0.80, "B-"),
    (0.77, "C+"),
    (0.73, "C"),
    (0.70, "C-"),
    (0.60, "D"),
    (0.0, "F"),
]


def _letter_for(score: float) -> str:
    for threshold, letter in _LETTER_THRESHOLDS:
        if score >= threshold:
            return letter
    return "F"


# ---------------------------------------------------------------------------
# Frontmatter parsing (shared util)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _strip_frontmatter(text: str) -> str:
    match = _FRONTMATTER_RE.match(text)
    return text[match.end() :] if match else text


def _approx_tokens(text: str) -> int:
    """Cheap token estimate: ~4 chars per token for English prose."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Per-artifact rubrics
# ---------------------------------------------------------------------------

ArtifactChecker = Callable[[str, Path], bool]


def _has_h1(text: str, _: Path) -> bool:
    return any(line.startswith("# ") for line in text.splitlines()[:20])


def _starts_with_problem(text: str, _: Path) -> bool:
    body = _strip_frontmatter(text)[:800].lower()
    return any(marker in body for marker in ("problem", "trigger", "when ", "symptom"))


def _has_when_to_use(text: str, _: Path) -> bool:
    lower = text.lower()
    return "## when to use" in lower or "## when to reach" in lower


def _has_fenced_code(text: str, _: Path) -> bool:
    return "```" in text


def _has_relative_link(text: str, _: Path) -> bool:
    for match in re.finditer(r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)", text):
        target = match.group(2).strip()
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        if "{{" in target or "<" in target:
            continue
        return True
    return False


def _reference_length_ok(text: str, _: Path) -> bool:
    body_tokens = _approx_tokens(_strip_frontmatter(text))
    return 1500 <= body_tokens <= 5000


def _has_frontmatter_description(text: str, _: Path) -> bool:
    fm = _parse_frontmatter(text)
    desc = fm.get("description")
    return isinstance(desc, str) and bool(desc.strip())


def _has_frontmatter_name(text: str, _: Path) -> bool:
    fm = _parse_frontmatter(text)
    name = fm.get("name")
    return isinstance(name, str) and bool(name.strip())


def _has_steps_section(text: str, _: Path) -> bool:
    lower = text.lower()
    return "## steps" in lower or re.search(r"^\s*\d+\.\s", text, re.MULTILINE) is not None


def _has_imperative_voice(text: str, _: Path) -> bool:
    body = _strip_frontmatter(text)
    return bool(re.search(r"\b(Read|Grep|Use|Run|Check|Write|Edit|Find)\b", body))


def _has_verify_section(text: str, _: Path) -> bool:
    return "## verify" in text.lower()


def _command_length_ok(text: str, _: Path) -> bool:
    body_tokens = _approx_tokens(_strip_frontmatter(text))
    return 400 <= body_tokens <= 2500


def _has_role_section(text: str, _: Path) -> bool:
    lower = text.lower()
    return "## role" in lower or "## what this agent" in lower


def _has_principles_section(text: str, _: Path) -> bool:
    lower = text.lower()
    return "## principles" in lower or "## what this agent checks" in lower


def _has_procedure_section(text: str, _: Path) -> bool:
    return "## procedure" in text.lower()


def _output_format_examples_use_backticks(text: str, _: Path) -> bool:
    """Catches the RUB-74 bug: output-format examples using markdown links.

    If the file has an "## Output format" heading, flag any markdown links
    in the section that follows — illustrative filenames should be in
    backticks, not wrapped in ``[file](file)`` links.
    """
    lower = text.lower()
    idx = lower.find("## output format")
    if idx == -1:
        return True  # not applicable
    section = text[idx : idx + 4000]
    next_section = re.search(r"\n## ", section[3:])
    if next_section:
        section = section[: next_section.start() + 3]
    return not re.search(r"(?<!\!)\[[^\]]+\]\([^)]+\)", section)


def _has_table(text: str, _: Path) -> bool:
    return bool(re.search(r"^\s*\|.+\|\s*$", text, re.MULTILINE))


def _no_placeholder_strings(text: str, _: Path) -> bool:
    return "lorem ipsum" not in text.lower() and "placeholder" not in text.lower()


def _skill_length_ok(text: str, _: Path) -> bool:
    body_tokens = _approx_tokens(_strip_frontmatter(text))
    return 2000 <= body_tokens <= 4500


def _has_allowed_tools(text: str, _: Path) -> bool:
    fm = _parse_frontmatter(text)
    return "allowed-tools" in fm or "allowed_tools" in fm


_RUBRICS: dict[ArtifactType, list[tuple[str, ArtifactChecker]]] = {
    ArtifactType.REFERENCE: [
        ("has H1 heading", _has_h1),
        ("starts with problem framing", _starts_with_problem),
        ("has 'When to use' section", _has_when_to_use),
        ("has fenced code block", _has_fenced_code),
        ("has relative markdown link", _has_relative_link),
        ("body length in 1500-5000 tokens", _reference_length_ok),
    ],
    ArtifactType.COMMAND: [
        ("has frontmatter description", _has_frontmatter_description),
        ("has steps section", _has_steps_section),
        ("uses imperative voice", _has_imperative_voice),
        ("has 'Verify' section", _has_verify_section),
        ("body length in 400-2500 tokens", _command_length_ok),
    ],
    ArtifactType.AGENT: [
        ("has frontmatter name", _has_frontmatter_name),
        ("has frontmatter description", _has_frontmatter_description),
        ("has 'Role' section", _has_role_section),
        ("has 'Principles' section", _has_principles_section),
        ("has 'Procedure' section", _has_procedure_section),
        ("output format examples use backticks", _output_format_examples_use_backticks),
    ],
    ArtifactType.SKILL: [
        ("has frontmatter name", _has_frontmatter_name),
        ("has frontmatter description", _has_frontmatter_description),
        ("has allowed-tools", _has_allowed_tools),
        ("has a markdown table", _has_table),
        ("no placeholder strings", _no_placeholder_strings),
        ("body length in 2000-4500 tokens", _skill_length_ok),
    ],
}


# ---------------------------------------------------------------------------
# Grading entry point
# ---------------------------------------------------------------------------


def grade_artifact(artifact: Artifact, plugin_root: Path) -> ArtifactGrade:
    """Score one artifact against its type-specific rubric."""
    path = plugin_root / artifact.path
    rubric = _RUBRICS.get(artifact.type, [])

    if not path.exists() or not rubric:
        return ArtifactGrade(
            artifact_id=artifact.id,
            artifact_type=artifact.type.value,
            path=artifact.path,
            score=0.0 if rubric else 1.0,
            letter="F" if rubric else "A",
            checks=[],
        )

    text = path.read_text(encoding="utf-8")
    checks = [RubricCheck(name=name, passed=fn(text, path)) for name, fn in rubric]
    passed = sum(1 for c in checks if c.passed)
    score = passed / len(checks)
    return ArtifactGrade(
        artifact_id=artifact.id,
        artifact_type=artifact.type.value,
        path=artifact.path,
        score=score,
        letter=_letter_for(score),
        checks=checks,
    )


def grade_run(
    run_dir: Path,
    *,
    failed_stages: list[str] | None = None,
) -> RunGrade:
    """Produce a complete grade card for a run directory.

    Requires the run's ``plan.json`` and the assembled plugin tree under
    ``output/<plugin_name>/`` to exist. Raises ``FileNotFoundError`` if
    they don't — callers should only call this after a successful
    ``franklin assemble`` (or explicitly handle the missing-tree case).
    """
    run = RunDirectory(run_dir)
    plan = run.load_plan()
    plugin_root = run.output_dir / plan.plugin.name
    if not plugin_root.exists():
        raise FileNotFoundError(f"no assembled plugin tree at {plugin_root}")

    broken_links = validate_links(plugin_root)
    template_leaks = find_template_leaks(plugin_root)
    frontmatter_issues = validate_frontmatter(plugin_root)
    markdown_files = sum(1 for _ in plugin_root.rglob("*.md"))

    totals = ValidatorTotals(
        broken_links=len(broken_links),
        template_leaks=len(template_leaks),
        frontmatter_issues=len(frontmatter_issues),
        markdown_files=markdown_files,
    )

    # Validator score: start at 1.0, subtract a penalty per issue scaled by
    # file count. Clamped to [0, 1].
    if markdown_files:
        validator_score = max(0.0, 1.0 - totals.total_issues / markdown_files)
    else:
        validator_score = 0.0

    coverage_fraction = _coverage_fraction(plan)

    artifact_grades = [
        grade_artifact(a, plugin_root) for a in plan.artifacts if a.type in _RUBRICS
    ]
    if artifact_grades:
        structural_average = sum(g.score for g in artifact_grades) / len(artifact_grades)
    else:
        structural_average = 0.0

    composite = 0.35 * validator_score + 0.20 * coverage_fraction + 0.45 * structural_average

    failed = list(failed_stages or [])
    if failed:
        composite = min(composite, 0.55)  # force F-range

    warnings = _collect_warnings(
        broken_links=broken_links,
        template_leaks=template_leaks,
        frontmatter_issues=frontmatter_issues,
        coverage_fraction=coverage_fraction,
        plugin_root=plugin_root,
    )

    return RunGrade(
        run_dir=str(run_dir),
        plugin_name=plan.plugin.name,
        graded_at=datetime.now(UTC),
        composite_score=round(composite, 3),
        letter=_letter_for(composite),
        validator_totals=totals,
        coverage_fraction=round(coverage_fraction, 3),
        structural_average=round(structural_average, 3),
        artifact_grades=artifact_grades,
        failed_stages=failed,
        warnings=warnings,
    )


def _coverage_fraction(plan: PlanManifest) -> float:
    """Fraction of artifacts whose feeds_from list is non-empty."""
    gradeable = [a for a in plan.artifacts if a.type in _RUBRICS]
    if not gradeable:
        return 0.0
    wired = sum(1 for a in gradeable if a.feeds_from)
    return wired / len(gradeable)


def _collect_warnings(
    *,
    broken_links: list[BrokenLink],
    template_leaks: list[TemplateLeak],
    frontmatter_issues: list[FrontmatterIssue],
    coverage_fraction: float,
    plugin_root: Path,
) -> list[str]:
    warnings: list[str] = []
    if broken_links:
        warnings.append(f"{len(broken_links)} broken markdown link(s)")
    if template_leaks:
        warnings.append(f"{len(template_leaks)} unfilled template placeholder(s)")
    if frontmatter_issues:
        warnings.append(f"{len(frontmatter_issues)} frontmatter issue(s)")
    if coverage_fraction < 1.0:
        warnings.append(
            f"{int((1 - coverage_fraction) * 100)}% of artifacts have empty feeds_from"
        )
    return warnings


def write_metrics(run_dir: Path, grade: RunGrade) -> Path:
    """Persist the grade card to ``runs/<slug>/metrics.json``."""
    import json

    metrics_path = run_dir / "metrics.json"
    metrics_path.write_text(json.dumps(grade.to_metrics_dict(), indent=2, default=str))
    return metrics_path
