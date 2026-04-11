"""PDF ingest: read a .pdf file into franklin's normalized chapter shape.

Uses pdfplumber for layout-aware text extraction. Chapter boundaries come
from the PDF outline when present (level-2 entries by default, since
level-1 in technical books is usually "parts"), falling back to a
font-size heading heuristic when no outline exists.

Unlike EPUB ingest, PDF extraction is inherently heuristic — glyphs are
positioned on pages rather than semantically structured — so output
quality depends on the source document. Code blocks are detected via
monospace font runs, prose via non-monospace runs, and page headers /
footers are filtered by vertical position. See RUB-88 for scope and
follow-up tickets (Tier 4 LLM cleanup, Tier 3 vision mode, etc).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pdfplumber
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfpage import PDFPage
from pdfminer.pdftypes import PDFObjRef, resolve1

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

# Font substrings that reliably indicate monospace (code).
_MONOSPACE_HINTS: tuple[str, ...] = (
    "courier",
    "cour",
    "mono",
    "typewriter",
    "consolas",
    "sourcecode",
    "inconsolata",
    "fira",
)

# Heading heuristic: a heading's point size must exceed the dominant body
# size by at least this many points for the heading detector to count it.
_HEADING_SIZE_MIN_DELTA = 2.0

# Chapters whose extracted prose has fewer words than this threshold are
# treated as spurious (e.g., an outline entry that points at the middle
# of a page and leaves no extractable text before the next boundary).
_MIN_CHAPTER_WORDS = 30

# Page-furniture cutoffs: words whose top coordinate (pdfplumber's y=0 is
# the top of the page) falls in the first _HEADER_BAND or last _FOOTER_BAND
# points of the page are filtered out. These bands are conservative —
# headers and footers typically live within 50 points of the edge.
_HEADER_BAND = 40.0
_FOOTER_BAND = 80.0


@dataclass(frozen=True)
class _OutlineEntry:
    level: int
    title: str
    start_page: int  # 1-indexed


def ingest_pdf(pdf_path: Path) -> tuple[BookManifest, list[NormalizedChapter]]:
    """Parse a PDF file into a manifest plus a list of normalized chapters.

    Discovers chapter boundaries via the PDF outline (level-2 preferred,
    level-1 fallback), or via a font-size heading heuristic when no outline
    is present. Each chapter's text is split into prose and code blocks
    based on monospace-font detection, with page headers and footers
    filtered by vertical position.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    with pdfplumber.open(str(pdf_path)) as pdf:
        metadata = _extract_metadata(pdf, pdf_path)
        outline_entries = _discover_chapters(pdf)
        chapters = _build_chapters(pdf, outline_entries)
        total_pages = len(pdf.pages)

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
            path=str(pdf_path),
            sha256=_hash_file(pdf_path),
            format="pdf",
            ingested_at=datetime.now(UTC),
        ),
        metadata=metadata,
        structure=structure,
    )
    _ = total_pages  # kept for future logging hooks
    return manifest, chapters


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def _extract_metadata(pdf: Any, pdf_path: Path) -> BookMetadata:
    md = pdf.metadata or {}
    title = _decode_meta(md.get("Title")) or pdf_path.stem
    author = _decode_meta(md.get("Author")) or ""
    return BookMetadata(
        title=title,
        authors=[author] if author else [],
        publisher=_decode_meta(md.get("Producer")),
        published=_decode_meta(md.get("CreationDate")),
        language=None,
    )


def _decode_meta(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            decoded = value.decode("utf-8", errors="replace").strip()
        except UnicodeDecodeError:
            return None
        return decoded or None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# Chapter discovery
# ---------------------------------------------------------------------------


def _discover_chapters(pdf: Any) -> list[_OutlineEntry]:
    """Return chapter boundary entries, preferring the outline over heuristics."""
    outline_entries = _extract_outline(pdf)
    if outline_entries:
        return outline_entries
    return _detect_chapters_by_font(pdf)


def _extract_outline(pdf: Any) -> list[_OutlineEntry]:
    """Walk the PDF outline and return chapter entries resolved to page numbers.

    Tries level 2 first (typical for technical books where level 1 is
    "parts"), falls back to level 1 if level 2 has fewer than 3 entries.
    Returns an empty list when neither level yields usable entries.
    """
    doc: PDFDocument = pdf.doc
    try:
        outlines = list(doc.get_outlines())
    except Exception:
        return []
    if not outlines:
        return []

    objid_to_page: dict[int, int] = {}
    for i, pmpage in enumerate(PDFPage.create_pages(doc)):
        objid_to_page[cast(int, pmpage.pageid)] = i + 1

    def resolve_named_dest(name: bytes) -> int | None:
        try:
            ref = doc.get_dest(name)
        except Exception:
            return None
        if ref is None:
            return None
        try:
            obj = resolve1(ref)
        except Exception:
            return None
        arr = obj.get(b"D") or obj.get("D") if isinstance(obj, dict) else obj
        if isinstance(arr, list) and arr and isinstance(arr[0], PDFObjRef):
            return objid_to_page.get(arr[0].objid)
        return None

    def page_for_action(action: Any) -> int | None:
        if action is None:
            return None
        try:
            resolved = resolve1(action)
        except Exception:
            return None
        if not isinstance(resolved, dict):
            return None
        d = resolved.get(b"D") or resolved.get("D")
        if d is None:
            return None
        if isinstance(d, bytes):
            return resolve_named_dest(d)
        if isinstance(d, list) and d and isinstance(d[0], PDFObjRef):
            return objid_to_page.get(d[0].objid)
        return None

    def collect(level: int) -> list[_OutlineEntry]:
        collected: list[_OutlineEntry] = []
        for entry_level, title, _dest, action, _se in outlines:
            if entry_level != level:
                continue
            page = page_for_action(action)
            if page is None:
                continue
            collected.append(
                _OutlineEntry(
                    level=entry_level,
                    title=str(title).strip() or "(untitled)",
                    start_page=page,
                )
            )
        return collected

    for target_level in (2, 1):
        entries = collect(target_level)
        if len(entries) >= 3:
            return entries
    return []


def _detect_chapters_by_font(pdf: Any) -> list[_OutlineEntry]:
    """Fallback chapter detection: largest font run at the top of a page is a heading."""
    size_counts: dict[float, int] = {}
    for page in pdf.pages:
        for char in page.chars:
            size = round(char.get("size", 0), 1)
            size_counts[size] = size_counts.get(size, 0) + 1
    if not size_counts:
        return []

    body_size = max(size_counts.items(), key=lambda item: item[1])[0]
    heading_min = body_size + _HEADING_SIZE_MIN_DELTA

    entries: list[_OutlineEntry] = []
    for page_num, page in enumerate(pdf.pages, start=1):
        words = page.extract_words(extra_attrs=["size"])
        if not words:
            continue
        topmost = min(words, key=lambda w: w["top"])
        if round(topmost["size"], 1) < heading_min:
            continue
        heading_tokens = [
            w["text"]
            for w in words
            if abs(w["top"] - topmost["top"]) < 3 and round(w["size"], 1) >= heading_min
        ]
        title = " ".join(heading_tokens).strip()
        if title:
            entries.append(_OutlineEntry(level=1, title=title, start_page=page_num))
    return entries


# ---------------------------------------------------------------------------
# Chapter extraction
# ---------------------------------------------------------------------------


def _build_chapters(pdf: Any, outline_entries: list[_OutlineEntry]) -> list[NormalizedChapter]:
    """Extract prose and code blocks per chapter, driven by outline boundaries."""
    total_pages = len(pdf.pages)

    if not outline_entries:
        outline_entries = [
            _OutlineEntry(
                level=1,
                title=(pdf.metadata or {}).get("Title", "Document"),
                start_page=1,
            )
        ]

    chapters: list[NormalizedChapter] = []
    for i, entry in enumerate(outline_entries):
        start = entry.start_page
        end = (
            outline_entries[i + 1].start_page - 1 if i + 1 < len(outline_entries) else total_pages
        )
        if end < start:
            continue

        text_parts: list[str] = []
        code_blocks: list[CodeBlock] = []
        for page_num in range(start, end + 1):
            page_idx = page_num - 1
            if page_idx < 0 or page_idx >= total_pages:
                continue
            page_prose, page_code = _extract_page_content(pdf.pages[page_idx])
            if page_prose:
                text_parts.append(page_prose)
            code_blocks.extend(page_code)

        text = "\n\n".join(text_parts).strip()
        word_count = len(text.split())
        if word_count < _MIN_CHAPTER_WORDS and i + 1 < len(outline_entries):
            continue

        order = len(chapters) + 1
        chapters.append(
            NormalizedChapter(
                chapter_id=f"ch{order:02d}",
                title=entry.title,
                order=order,
                source_ref=f"pp. {start}-{end}",
                word_count=word_count,
                text=text,
                code_blocks=code_blocks,
                headings=[entry.title],
            )
        )
    return chapters


def _extract_page_content(page: Any) -> tuple[str, list[CodeBlock]]:
    """Return (prose, code_blocks) for one page, filtering page furniture.

    Uses ``x_tolerance=2`` when extracting words — the pdfplumber default
    of 3 is too aggressive on PDFs with tight kerning and produces jumbled
    multi-word tokens like "ButthereisonepartofeveryappthatRails" instead
    of individual words.

    Code block detection works line-by-line: a visual line is classified
    as a code line when at least half its words are in a monospace font
    AND the line contains at least two words. Consecutive code lines
    merge into a single CodeBlock. Inline code mentions (a single Courier
    word dropped into a prose sentence) stay in the prose, which is what
    a reader would expect.
    """
    try:
        words = page.extract_words(extra_attrs=["fontname", "size"], x_tolerance=2)
    except Exception:
        return "", []
    if not words:
        return "", []

    page_height = float(page.height)
    body_words: list[dict[str, Any]] = []
    for word in words:
        top = float(word["top"])
        if top < _HEADER_BAND or top > page_height - _FOOTER_BAND:
            continue
        body_words.append(word)
    if not body_words:
        return "", []

    body_words.sort(key=lambda w: (w["top"], w["x0"]))

    # Group words into visual lines by top-clustering.
    lines: list[list[dict[str, Any]]] = []
    current_line: list[dict[str, Any]] = []
    current_top: float | None = None
    for word in body_words:
        top = float(word["top"])
        if current_top is None or abs(top - current_top) < 3:
            current_line.append(word)
            if current_top is None:
                current_top = top
        else:
            if current_line:
                lines.append(current_line)
            current_line = [word]
            current_top = top
    if current_line:
        lines.append(current_line)

    prose_lines: list[str] = []
    code_buffer: list[str] = []
    code_blocks: list[CodeBlock] = []

    def flush_code_buffer() -> None:
        if code_buffer:
            code_blocks.append(CodeBlock(language=None, code="\n".join(code_buffer)))
            code_buffer.clear()

    for line in lines:
        line_text = " ".join(w["text"] for w in line)
        mono_count = sum(1 for w in line if _is_monospace(w.get("fontname", "")))
        is_code_line = len(line) >= 2 and mono_count >= (len(line) + 1) // 2
        if is_code_line:
            code_buffer.append(line_text)
        else:
            flush_code_buffer()
            prose_lines.append(line_text)
    flush_code_buffer()

    prose = "\n".join(prose_lines).strip()
    return prose, code_blocks


def _is_monospace(fontname: str) -> bool:
    lowered = fontname.lower()
    return any(hint in lowered for hint in _MONOSPACE_HINTS)


# ---------------------------------------------------------------------------
# File hashing (shared idiom with ingest_epub)
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
