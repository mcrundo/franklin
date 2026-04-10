"""EPUB ingest: read a .epub file into Franklin's normalized shape.

This stage is pure — no LLM calls, deterministic output. Given the same
EPUB, it always produces the same BookManifest and chapter list.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag

from bs4 import BeautifulSoup
from bs4.element import Tag
from ebooklib import ITEM_DOCUMENT, epub  # type: ignore[import-untyped]

from franklin import __version__
from franklin.schema import (
    BookManifest,
    BookMetadata,
    BookSource,
    BookStructure,
    CodeBlock,
    NormalizedChapter,
    TocEntry,
)


def ingest_epub(epub_path: Path) -> tuple[BookManifest, list[NormalizedChapter]]:
    """Parse an EPUB file into a manifest plus a list of normalized chapters.

    Chapters are discovered via the book's TOC. Files pointed to by multiple
    TOC entries are deduplicated (first entry wins); empty files are skipped.

    Raises FileNotFoundError if the epub path does not exist.
    """
    if not epub_path.exists():
        raise FileNotFoundError(epub_path)

    book: Any = epub.read_epub(str(epub_path), options={"ignore_ncx": False})

    metadata = _extract_metadata(book)
    chapters = _extract_chapters(book)

    structure = BookStructure(
        toc=[
            TocEntry(
                id=c.chapter_id,
                title=c.title,
                level=1,
                word_count=c.word_count,
                source_ref=c.source_ref,
            )
            for c in chapters
        ],
        total_chapters=len(chapters),
        total_words=sum(c.word_count for c in chapters),
        has_code_examples=any(c.code_blocks for c in chapters),
        has_exercises=any("exercise" in c.title.lower() for c in chapters),
        has_glossary=any("glossary" in c.title.lower() for c in chapters),
    )

    manifest = BookManifest(
        franklin_version=__version__,
        source=BookSource(
            path=str(epub_path),
            sha256=_hash_file(epub_path),
            format="epub",
            ingested_at=datetime.now(UTC),
        ),
        metadata=metadata,
        structure=structure,
    )
    return manifest, chapters


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def _extract_metadata(book: Any) -> BookMetadata:
    def first(name: str) -> str | None:
        items = book.get_metadata("DC", name)
        if not items:
            return None
        value = items[0][0]
        return value or None

    def all_values(name: str) -> list[str]:
        return [v[0] for v in book.get_metadata("DC", name) if v and v[0]]

    return BookMetadata(
        title=first("title") or "Untitled",
        authors=all_values("creator"),
        publisher=first("publisher"),
        published=first("date"),
        isbn=first("identifier"),
        language=first("language"),
    )


# ---------------------------------------------------------------------------
# Chapters
# ---------------------------------------------------------------------------


def _extract_chapters(book: Any) -> list[NormalizedChapter]:
    entries = _flatten_toc(book.toc)
    if not entries:
        entries = _spine_entries(book)

    chapters: list[NormalizedChapter] = []
    seen_files: set[str] = set()

    for title, href in entries:
        file_href, _fragment = _split_href(href)
        if file_href in seen_files:
            continue
        seen_files.add(file_href)

        item = book.get_item_with_href(file_href)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue

        html = item.get_body_content().decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        text, code_blocks, headings = _extract_content(soup)

        if not text.strip():
            continue

        order = len(chapters) + 1
        chapters.append(
            NormalizedChapter(
                chapter_id=f"ch{order:02d}",
                title=title or headings[0] if headings else (title or file_href),
                order=order,
                source_ref=file_href,
                word_count=len(text.split()),
                text=text,
                code_blocks=code_blocks,
                headings=headings,
            )
        )
    return chapters


def _flatten_toc(toc: Any) -> list[tuple[str, str]]:
    """Walk ebooklib's nested TOC structure into flat (title, href) pairs.

    TOC entries are either epub.Link objects or (Section, [children]) tuples.
    A Section with an href is included alongside its children.
    """
    out: list[tuple[str, str]] = []
    for entry in toc or []:
        if isinstance(entry, epub.Link):
            if entry.href:
                out.append((entry.title or "", entry.href))
        elif isinstance(entry, tuple) and len(entry) == 2:
            section, children = entry
            href = getattr(section, "href", None)
            if href:
                out.append((getattr(section, "title", "") or "", href))
            out.extend(_flatten_toc(children))
    return out


def _spine_entries(book: Any) -> list[tuple[str, str]]:
    """Fallback when a book has no TOC: derive chapters from the spine."""
    out: list[tuple[str, str]] = []
    for item_id, _linear in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue
        html = item.get_body_content().decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        heading = soup.find(["h1", "h2"])
        title = heading.get_text(strip=True) if heading else item.file_name
        out.append((title, item.file_name))
    return out


def _split_href(href: str) -> tuple[str, str | None]:
    base, frag = urldefrag(href)
    return unquote(base), (frag or None)


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


def _extract_content(soup: BeautifulSoup) -> tuple[str, list[CodeBlock], list[str]]:
    """Pull clean text, code blocks, and headings out of a parsed chapter."""
    # Drop things that never belong in extracted text.
    for tag_name in ("script", "style", "nav"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Lift out code blocks before the text pass so they don't pollute prose.
    code_blocks: list[CodeBlock] = []
    for pre in soup.find_all("pre"):
        if not isinstance(pre, Tag):
            continue
        code_elem = pre.find("code") or pre
        code = code_elem.get_text()
        if not code.strip():
            pre.decompose()
            continue
        language = _language_from_classes(code_elem)
        code_blocks.append(CodeBlock(language=language, code=code))
        pre.decompose()

    headings = [
        h.get_text(strip=True)
        for h in soup.find_all(["h1", "h2", "h3", "h4"])
        if h.get_text(strip=True)
    ]

    raw = soup.get_text(separator="\n", strip=True)
    cleaned = "\n".join(line for line in (ln.strip() for ln in raw.splitlines()) if line)
    return cleaned, code_blocks, headings


def _language_from_classes(element: Any) -> str | None:
    classes = element.get("class") if hasattr(element, "get") else None
    if not classes:
        return None
    for cls in classes:
        cls_str = str(cls)
        if cls_str.startswith("language-"):
            return cls_str.removeprefix("language-")
        if cls_str.startswith("lang-"):
            return cls_str.removeprefix("lang-")
    return None


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
