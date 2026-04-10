"""Stage 1: parse a book file into normalized chapters and a partial manifest."""

from franklin.ingest.epub import ingest_epub

__all__ = ["ingest_epub"]
