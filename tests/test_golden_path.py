"""Golden-path end-to-end regression oracle for RUB-95.

Drives ingest → map → plan → reduce → assemble through each stage's
public API with a single scripted fake Anthropic client. The fake
dispatches tool-use responses by ``tool_name`` so one client handles
every stage.

The refactor to stage services (RUB-98..RUB-104) will swap the
orchestration layer but keep the stage-function contracts identical.
If this test still passes after each sub-issue lands, the refactor is
safe.

The fixture EPUB is sliced to two chapters to keep the test fast; all
LLM output is synthetic but conforms to the Pydantic contracts between
stages.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from franklin.assembler import (
    validate_frontmatter,
    validate_links,
    write_plugin_manifest,
)
from franklin.ingest import ingest_epub
from franklin.mapper import extract_chapter
from franklin.planner import design_plan
from franklin.reducer import generate_artifact
from franklin.schema import (
    ChapterSidecar,
    PlanManifest,
)

FIXTURE_EPUB = Path(__file__).resolve().parents[1] / (
    "Layered Design for Ruby on Rails Applications by Vladimir Dementyev.epub"
)


# ---------------------------------------------------------------------------
# Scripted fake Anthropic client
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, response: Any) -> None:
        self._response = response

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._response


class _ScriptedClient:
    """Dispatches fake tool-use responses by ``tool_name``.

    Each stage's call_tool() selects a tool; this fake looks at the
    ``tool_choice`` kwarg and returns the scripted payload for that
    tool. Callers register payload-factories that can inspect the
    kwargs (e.g. to tailor extraction output to the current chapter).
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Any] = {}
        self.messages = self
        self.calls: list[dict[str, Any]] = []

    def register(self, tool_name: str, factory: Any) -> None:
        self._handlers[tool_name] = factory

    @contextmanager
    def stream(self, **kwargs: Any) -> Iterator[_FakeStream]:
        self.calls.append(kwargs)
        tool_name = kwargs["tool_choice"]["name"]
        payload = self._handlers[tool_name](kwargs)
        yield _FakeStream(
            SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", input=payload)],
                stop_reason="tool_use",
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=50,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )
        )


# ---------------------------------------------------------------------------
# Scripted payload factories
# ---------------------------------------------------------------------------


def _chapter_extraction_for(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Produce a minimal-but-valid ChapterExtraction payload.

    The user prompt embeds the chapter id; scrape it so every chapter
    gets unique ids.
    """
    user = kwargs["messages"][0]["content"]
    text = (
        user
        if isinstance(user, str)
        else "".join(block.get("text", "") for block in user if isinstance(block, dict))
    )
    # Chapter ids look like 'ch01', 'ch02' etc. in the prompt header.
    cid = "ch00"
    for token in text.split():
        if token.startswith("ch") and len(token) >= 4 and token[2:4].isdigit():
            cid = token[:4]
            break
    return {
        "summary": f"Fake summary for {cid}.",
        "concepts": [
            {
                "id": f"{cid}.concept.core",
                "name": "Core Concept",
                "definition": "A thing the chapter defines.",
                "importance": "high",
                "source_location": f"{cid} §1",
            }
        ],
    }


def _plan_proposal(_kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "plugin": {
            "name": "golden-path",
            "version": "0.1.0",
            "description": "Golden-path test plugin",
            "keywords": ["test"],
        },
        "planner_rationale": "Single skill covering the two sampled chapters.",
        "artifacts": [
            {
                "id": "art.skill.root",
                "type": "skill",
                "path": "skills/golden-path/SKILL.md",
                "brief": "Entry skill routing to references.",
                "feeds_from": ["book.metadata"],
                "estimated_output_tokens": 1000,
            },
        ],
        "coherence_rules": ["Be consistent."],
        "skipped_artifact_types": [],
        "estimated_total_output_tokens": 1000,
        "estimated_reduce_calls": 1,
    }


def _artifact_content(_kwargs: dict[str, Any]) -> dict[str, Any]:
    body = (
        "---\n"
        "name: golden-path\n"
        'description: "Entry skill for the golden-path test plugin"\n'
        "---\n\n"
        "# Golden Path\n\n"
        "Covers the core concept sampled from two chapters.\n"
    )
    return {"content": body}


# ---------------------------------------------------------------------------
# Golden-path test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FIXTURE_EPUB.exists(), reason="fixture EPUB not present")
def test_golden_path_ingest_to_assemble(tmp_path: Path) -> None:
    client = _ScriptedClient()
    client.register("save_chapter_extraction", _chapter_extraction_for)
    client.register("save_plan_proposal", _plan_proposal)
    client.register("save_artifact_file", _artifact_content)

    # --- ingest -------------------------------------------------------------
    manifest, all_chapters = ingest_epub(FIXTURE_EPUB)
    # Slice to the first two chapters to keep the test fast. The TOC is
    # not guaranteed to be sorted 1:1 with chapters[], so we slice chapters
    # directly and filter the TOC to match.
    chapters = all_chapters[:2]
    keep_ids = {c.chapter_id for c in chapters}
    manifest.structure.toc = [e for e in manifest.structure.toc if e.id in keep_ids]

    # --- map ----------------------------------------------------------------
    sidecars: dict[str, ChapterSidecar] = {}
    for chapter in chapters:
        sidecar, _, _ = extract_chapter(manifest, chapter, client=client)
        sidecars[sidecar.chapter_id] = sidecar

    assert len(sidecars) == 2

    # --- plan ---------------------------------------------------------------
    plan, _, _ = design_plan(manifest, list(sidecars.values()), client=client)
    assert isinstance(plan, PlanManifest)
    assert plan.plugin.name == "golden-path"
    assert len(plan.artifacts) == 1

    # --- reduce -------------------------------------------------------------
    plugin_root = tmp_path / "output" / plan.plugin.name
    for artifact in plan.artifacts:
        result = generate_artifact(
            artifact,
            plan=plan,
            book=manifest,
            sidecars=sidecars,
            client=client,
        )
        dest = plugin_root / artifact.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(result.content, encoding="utf-8")

    # --- assemble -----------------------------------------------------------
    manifest_path = write_plugin_manifest(plugin_root, plan.plugin)
    assert manifest_path.exists()
    assert '"name": "golden-path"' in manifest_path.read_text(encoding="utf-8")

    # Validate the pieces the assemble stage gates on. Golden path means
    # zero issues: missing frontmatter or broken internal links would
    # fail the assemble stage in production.
    assert validate_frontmatter(plugin_root) == []
    assert validate_links(plugin_root) == []

    # Sanity — the produced file is discoverable where the planner said.
    assert (plugin_root / "skills" / "golden-path" / "SKILL.md").exists()


def test_golden_path_records_expected_tool_calls(tmp_path: Path) -> None:
    """Smaller smoke test: confirm our scripted client wiring is correct.

    Uses only the mapper stage with a synthetic in-memory chapter so it
    runs in milliseconds and stays green even if the fixture EPUB is
    missing (CI environments that don't ship the fixture).
    """
    from datetime import UTC, datetime

    from franklin.schema import (
        BookManifest,
        BookMetadata,
        BookSource,
        BookStructure,
        NormalizedChapter,
    )

    book = BookManifest(
        franklin_version="0.1.0",
        source=BookSource(
            path="x.epub", sha256="0" * 64, format="epub", ingested_at=datetime.now(UTC)
        ),
        metadata=BookMetadata(title="Tiny", authors=["Ada"]),
        structure=BookStructure(),
    )
    chapter = NormalizedChapter(
        chapter_id="ch01",
        title="Opening",
        order=1,
        source_ref="x.xhtml",
        word_count=10,
        text="ch01 body text.",
    )

    client = _ScriptedClient()
    client.register("save_chapter_extraction", _chapter_extraction_for)

    sidecar, _, _ = extract_chapter(book, chapter, client=client)
    assert sidecar.chapter_id == "ch01"
    assert sidecar.summary == "Fake summary for ch01."
    assert len(client.calls) == 1
    assert client.calls[0]["tool_choice"]["name"] == "save_chapter_extraction"
