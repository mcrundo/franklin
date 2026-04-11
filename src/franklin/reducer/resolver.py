"""Walk an artifact's feeds_from paths into filtered sidecar data.

Each artifact in the plan declares a list of dotted paths like `ch07.anti_patterns`
or `book.metadata` describing which slice of the distilled book feeds its
generation. The resolver walks those paths into concrete Pydantic objects and
produces a ready-to-inject markdown rendering for the generator prompt.

Supported path forms:

    chNN                    — every category in one chapter's sidecar
    chNN.<category>         — one category from one chapter
    book.<field>            — one book-level field (metadata, classification,
                              cross_chapter_themes, glossary, structure)

Unknown or unresolvable paths are collected into `unresolved` rather than
raised, because a generator with a few missing feeds can usually still
produce a useful file — surfacing the skips to the caller is the right
failure mode for iteration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from franklin.schema import BookManifest, ChapterSidecar

_CATEGORY_FIELDS: tuple[str, ...] = (
    "concepts",
    "principles",
    "rules",
    "anti_patterns",
    "code_examples",
    "decision_rules",
    "actionable_workflows",
    "terminology",
    "cross_references",
)

_BOOK_FIELDS: frozenset[str] = frozenset(
    {"metadata", "classification", "cross_chapter_themes", "glossary", "structure"}
)


@dataclass
class ResolvedContext:
    """Filtered sidecar and book data for a single artifact generator.

    Carries ready-to-inject markdown for both halves of the prompt — the
    book-level header (title, authors, any explicitly requested book
    fields) and the per-chapter filtered content — plus the structured
    Pydantic objects for generators that want to walk the data directly.
    `unresolved` surfaces any feeds_from paths that couldn't be walked
    so callers can warn or log.
    """

    book_markdown: str
    chapters_markdown: str
    chapter_items: dict[str, dict[str, list[Any]]] = field(default_factory=dict)
    book_fields: dict[str, Any] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    markdown: str = field(init=False)

    def __post_init__(self) -> None:
        if self.chapters_markdown:
            self.markdown = f"{self.book_markdown}\n\n{self.chapters_markdown}".strip()
        else:
            self.markdown = self.book_markdown.strip()


def resolve_feeds(
    feeds_from: list[str],
    *,
    book: BookManifest,
    sidecars: dict[str, ChapterSidecar],
) -> ResolvedContext:
    """Resolve a list of feeds_from paths into a ResolvedContext.

    The returned markdown is always rooted with a small book header
    (title + authors) so every generator starts with minimal context
    about what book it's reading from, even if no `book.*` path was
    explicitly requested.
    """
    chapter_items: dict[str, dict[str, list[Any]]] = {}
    book_out: dict[str, Any] = {}
    unresolved: list[str] = []

    for path in feeds_from:
        _resolve_one(path, book, sidecars, chapter_items, book_out, unresolved)

    book_md = _render_book_header(book, book_out)
    chapters_md = _render_chapter_sections(sidecars, chapter_items)
    return ResolvedContext(
        book_markdown=book_md,
        chapters_markdown=chapters_md,
        chapter_items=chapter_items,
        book_fields=book_out,
        unresolved=unresolved,
    )


def _resolve_one(
    path: str,
    book: BookManifest,
    sidecars: dict[str, ChapterSidecar],
    chapter_items: dict[str, dict[str, list[Any]]],
    book_out: dict[str, Any],
    unresolved: list[str],
) -> None:
    parts = path.split(".", 2)
    if not parts or not parts[0]:
        unresolved.append(path)
        return

    root = parts[0]

    if root == "book":
        if len(parts) < 2 or parts[1] not in _BOOK_FIELDS:
            unresolved.append(path)
            return
        value = getattr(book, parts[1], None)
        if value is None:
            unresolved.append(path)
            return
        book_out[parts[1]] = value
        return

    if root not in sidecars:
        unresolved.append(path)
        return

    sidecar = sidecars[root]
    bucket = chapter_items.setdefault(root, {})

    if len(parts) == 1:
        for cat in _CATEGORY_FIELDS:
            items = getattr(sidecar, cat, None) or []
            if items:
                bucket[cat] = list(items)
        return

    category = parts[1]
    if category not in _CATEGORY_FIELDS:
        unresolved.append(path)
        return

    items = getattr(sidecar, category, None) or []
    bucket[category] = list(items)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_book_header(book: BookManifest, book_fields: dict[str, Any]) -> str:
    parts: list[str] = [f"# {book.metadata.title}"]
    if book.metadata.authors:
        parts.append(f"**Authors:** {', '.join(book.metadata.authors)}")
    parts.append("")

    if "classification" in book_fields and book.classification is not None:
        c = book.classification
        parts.append(f"**Classification.** {c.primary_intent} (audience: {c.audience})")
        parts.append("")

    if "cross_chapter_themes" in book_fields and book.cross_chapter_themes:
        parts.append("**Cross-chapter themes:**")
        for theme in book.cross_chapter_themes:
            parts.append(f"- {theme.theme} — {', '.join(theme.chapters)}")
        parts.append("")

    if "glossary" in book_fields and book.glossary:
        parts.append("**Glossary:**")
        for term, definition in book.glossary.items():
            parts.append(f"- **{term}**: {definition}")
        parts.append("")

    return "\n".join(parts).rstrip()


def _render_chapter_sections(
    sidecars: dict[str, ChapterSidecar],
    chapter_items: dict[str, dict[str, list[Any]]],
) -> str:
    parts: list[str] = []
    for chapter_id in sorted(chapter_items):
        sidecar = sidecars[chapter_id]
        categories = chapter_items[chapter_id]
        if not categories:
            continue

        parts.append(f"## {chapter_id}: {sidecar.title}")
        parts.append("")
        parts.append(f"**Summary.** {sidecar.summary}")
        parts.append("")

        for cat_name in _CATEGORY_FIELDS:
            items = categories.get(cat_name) or []
            if not items:
                continue
            parts.append(f"### {cat_name.replace('_', ' ').title()} ({len(items)})")
            parts.append("")
            for item in items:
                parts.append(_render_item(cat_name, item))
                if cat_name == "code_examples":
                    parts.append("")
            if cat_name != "code_examples":
                parts.append("")

    return "\n".join(parts).rstrip()


def _render_item(category: str, item: Any) -> str:
    if category == "concepts":
        return (
            f"- `{item.id}` **{item.name}** ({item.importance.value}) — "
            f"{item.definition} _({item.source_location})_"
        )

    if category == "principles":
        rationale = f" _({item.rationale})_" if item.rationale else ""
        return f"- `{item.id}` {item.statement}{rationale} _source: {item.source_location}_"

    if category == "rules":
        applies = f" (applies when: {item.applies_when})" if item.applies_when else ""
        exceptions = f" exceptions: {'; '.join(item.exceptions)}" if item.exceptions else ""
        return f"- `{item.id}` {item.rule}{applies}{exceptions} _source: {item.source_location}_"

    if category == "anti_patterns":
        lines = [f"- `{item.id}` **{item.name}**"]
        lines.append(f"  {item.description}")
        lines.append(f"  **Fix.** {item.fix}")
        if item.smell_signals:
            lines.append(f"  **Signals.** {'; '.join(item.smell_signals)}")
        if item.code_before_ref:
            lines.append(f"  **Before:** `{item.code_before_ref}`")
        if item.code_after_ref:
            lines.append(f"  **After:** `{item.code_after_ref}`")
        lines.append(f"  _source: {item.source_location}_")
        return "\n".join(lines)

    if category == "code_examples":
        lang = item.language or ""
        context = f"_{item.context}_\n" if item.context else ""
        return (
            f"#### `{item.id}` — {item.label}\n"
            f"{context}"
            f"_source: {item.source_location}_\n\n"
            f"```{lang}\n{item.code}\n```"
        )

    if category == "decision_rules":
        lines = [f"- `{item.id}` **{item.question}**"]
        if item.yes_when:
            lines.append(f"  **Yes when:** {'; '.join(item.yes_when)}")
        if item.no_when:
            lines.append(f"  **No when:** {'; '.join(item.no_when)}")
        lines.append(f"  _source: {item.source_location}_")
        return "\n".join(lines)

    if category == "actionable_workflows":
        lines = [f"- `{item.id}` **{item.name}**"]
        if item.trigger:
            lines.append(f"  **Trigger:** {item.trigger}")
        lines.append("  **Steps:**")
        for step in item.steps:
            lines.append(f"    - {step}")
        lines.append(f"  _source: {item.source_location}_")
        return "\n".join(lines)

    if category == "terminology":
        return f"- **{item.term}**: {item.definition} _({item.source_location})_"

    if category == "cross_references":
        return f"- → {item.to_chapter}: {item.reason}"

    return f"- {item!r}"
