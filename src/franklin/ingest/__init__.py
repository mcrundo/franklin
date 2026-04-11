"""Stage 1: parse a book file into normalized chapters and a partial manifest."""

from pathlib import Path

from franklin.ingest.epub import ingest_epub
from franklin.ingest.pdf import ingest_pdf
from franklin.schema import BookManifest, NormalizedChapter


class UnsupportedFormatError(ValueError):
    """Raised when a book file's extension is not recognized."""


def ingest_book(path: Path) -> tuple[BookManifest, list[NormalizedChapter]]:
    """Dispatch ingest by file extension: .epub or .pdf."""
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return ingest_epub(path)
    if suffix == ".pdf":
        return ingest_pdf(path)
    raise UnsupportedFormatError(
        f"unsupported book format {suffix!r} — franklin accepts .epub or .pdf"
    )


__all__ = ["UnsupportedFormatError", "ingest_book", "ingest_epub", "ingest_pdf"]
