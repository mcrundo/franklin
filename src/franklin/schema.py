"""Pydantic models for Franklin's pipeline data.

These models are the contract between stages. Every stage reads and writes
JSON files that conform to these types. Keep them stable.

File layout inside a run directory:

    runs/<slug>/
      book.json              # BookManifest — evolves across stages
      raw/chNN.json          # NormalizedChapter — written by ingest
      chapters/chNN.json     # ChapterSidecar — written by map
      plan.json              # PlanManifest — written by plan stage
      output/                # generated plugin tree — written by reduce/assemble
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BookType(StrEnum):
    REFERENCE = "reference"
    WORKFLOW = "workflow"
    PRINCIPLES = "principles"
    TUTORIAL = "tutorial"
    PATTERNS_AND_PRACTICES = "patterns_and_practices"
    OPINION = "opinion"
    UNKNOWN = "unknown"


class Importance(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ArtifactType(StrEnum):
    SKILL = "skill"
    REFERENCE = "reference"
    COMMAND = "command"
    AGENT = "agent"
    PLUGIN_MANIFEST = "plugin_manifest"


class ChapterKind(StrEnum):
    """Role of a chapter within the book.

    The map stage runs full extraction only on CONTENT and INTRODUCTION
    chapters; everything else is skipped or handled with a cheaper pass.
    """

    CONTENT = "content"
    INTRODUCTION = "introduction"
    PART_DIVIDER = "part_divider"
    FRONT_MATTER = "front_matter"
    BACK_MATTER = "back_matter"


# ---------------------------------------------------------------------------
# Raw ingest output
# ---------------------------------------------------------------------------


class CodeBlock(_Base):
    """A code block lifted from a chapter during ingest."""

    language: str | None = None
    code: str
    caption: str | None = None


class NormalizedChapter(_Base):
    """Cleaned chapter content written by the ingest stage.

    This is the raw material every later stage reads from. Sidecars written
    by the map stage are stored separately under chapters/.
    """

    chapter_id: str = Field(description="Stable slug like 'ch03'")
    title: str
    order: int
    source_ref: str = Field(description="Path or href into the original EPUB")
    word_count: int
    text: str = Field(description="Full cleaned plain text of the chapter")
    code_blocks: list[CodeBlock] = Field(default_factory=list)
    headings: list[str] = Field(
        default_factory=list,
        description="Section headings in document order, for structural hints",
    )


# ---------------------------------------------------------------------------
# Book manifest
# ---------------------------------------------------------------------------


class BookSource(_Base):
    path: str
    sha256: str
    format: str
    ingested_at: datetime


class BookMetadata(_Base):
    title: str
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list)
    publisher: str | None = None
    published: str | None = None
    isbn: str | None = None
    language: str | None = None


class TocEntry(_Base):
    id: str
    title: str
    level: int = 1
    word_count: int = 0
    source_ref: str
    kind: ChapterKind = ChapterKind.CONTENT
    kind_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    kind_reason: str = ""


class BookStructure(_Base):
    toc: list[TocEntry] = Field(default_factory=list)
    total_chapters: int = 0
    total_words: int = 0
    has_code_examples: bool = False
    has_exercises: bool = False
    has_glossary: bool = False


class Classification(_Base):
    """Populated by the map stage's intent pass over the introduction chapter."""

    domain: str
    subdomain: str | None = None
    book_type: BookType = BookType.UNKNOWN
    audience: str
    primary_intent: str
    confidence: float = Field(ge=0.0, le=1.0)


class CrossChapterTheme(_Base):
    theme: str
    chapters: list[str] = Field(description="List of chapter_id values")


class BookManifest(_Base):
    """The evolving book-level manifest.

    Ingest fills in source, metadata, and structure. The map stage adds
    classification, glossary, and cross_chapter_themes.
    """

    franklin_version: str
    source: BookSource
    metadata: BookMetadata
    structure: BookStructure
    classification: Classification | None = None
    glossary: dict[str, str] = Field(default_factory=dict)
    cross_chapter_themes: list[CrossChapterTheme] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Chapter sidecar (written by the map stage)
# ---------------------------------------------------------------------------


class Concept(_Base):
    id: str
    name: str
    definition: str
    importance: Importance = Importance.MEDIUM
    source_quote: str | None = None
    source_location: str


class Principle(_Base):
    id: str
    statement: str
    rationale: str | None = None
    source_location: str


class Rule(_Base):
    id: str
    rule: str
    applies_when: str | None = None
    exceptions: list[str] = Field(default_factory=list)
    source_location: str


class AntiPattern(_Base):
    id: str
    name: str
    description: str
    smell_signals: list[str] = Field(default_factory=list)
    fix: str
    code_before_ref: str | None = None
    code_after_ref: str | None = None
    source_location: str


class CodeExample(_Base):
    id: str
    language: str
    label: str
    code: str
    context: str | None = None
    annotations: list[str] = Field(default_factory=list)
    source_location: str


class DecisionRule(_Base):
    id: str
    question: str
    yes_when: list[str] = Field(default_factory=list)
    no_when: list[str] = Field(default_factory=list)
    source_location: str


class ActionableWorkflow(_Base):
    """Ordered step-by-step procedure; the direct input to a slash command."""

    id: str
    name: str
    trigger: str | None = None
    steps: list[str]
    source_location: str


class TerminologyEntry(_Base):
    term: str
    definition: str
    source_location: str


class CrossReference(_Base):
    to_chapter: str
    reason: str


class ChapterSidecar(_Base):
    """Structured extraction output for a single chapter, produced by the map stage."""

    chapter_id: str
    title: str
    order: int
    source_ref: str
    word_count: int
    summary: str

    concepts: list[Concept] = Field(default_factory=list)
    principles: list[Principle] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    anti_patterns: list[AntiPattern] = Field(default_factory=list)
    code_examples: list[CodeExample] = Field(default_factory=list)
    decision_rules: list[DecisionRule] = Field(default_factory=list)
    actionable_workflows: list[ActionableWorkflow] = Field(default_factory=list)
    terminology: list[TerminologyEntry] = Field(default_factory=list)
    cross_references: list[CrossReference] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Plan manifest (written by the plan stage)
# ---------------------------------------------------------------------------


class PluginMeta(_Base):
    name: str
    version: str = "0.1.0"
    description: str
    keywords: list[str] = Field(default_factory=list)


class Artifact(_Base):
    """One file the reduce stage will generate."""

    id: str
    type: ArtifactType
    path: str = Field(description="Relative path inside the output plugin tree")
    brief: str
    feeds_from: list[str] = Field(
        default_factory=list,
        description="Dotted paths into sidecars or book.json (e.g. 'ch03.concepts')",
    )
    estimated_output_tokens: int = 0


class SkippedArtifact(_Base):
    type: str
    reason: str


class PlanManifest(_Base):
    """The output of the plan stage — the contract the reduce stage consumes."""

    book_id: str
    generated_at: datetime
    planner_model: str
    planner_rationale: str

    plugin: PluginMeta
    artifacts: list[Artifact] = Field(default_factory=list)
    coherence_rules: list[str] = Field(default_factory=list)
    skipped_artifact_types: list[SkippedArtifact] = Field(default_factory=list)

    estimated_total_output_tokens: int = 0
    estimated_reduce_calls: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def dump_json(model: BaseModel) -> str:
    """Serialize a model to pretty JSON using Pydantic's encoder."""
    return model.model_dump_json(indent=2, exclude_none=False)


def parse_json[T: BaseModel](
    model_cls: type[T], data: str | bytes | dict[str, Any]
) -> T:
    """Parse JSON (or a dict) into a model instance."""
    if isinstance(data, dict):
        return model_cls.model_validate(data)
    return model_cls.model_validate_json(data)
