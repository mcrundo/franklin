"""Cost estimator for ``franklin run --estimate``.

Pure-Python heuristic predictor of what a full pipeline run will spend.
Ingest is free (local parsing); the paid stages are map, plan, reduce,
and optionally Tier 4 cleanup. The estimator walks a parsed
``BookManifest`` + chapter list, applies per-stage token heuristics,
and returns a ``RunEstimate`` dataclass.

The heuristics intentionally lean pessimistic (a little high) so the
"continue? [y/N]" prompt doesn't mislead the user into a surprise. Real
runs should land at or below the estimate, not above it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from franklin.schema import BookManifest, ChapterKind, NormalizedChapter

# ---------------------------------------------------------------------------
# Pricing (USD per million tokens)
# ---------------------------------------------------------------------------

# Sonnet 4 pricing used by map, plan, and cleanup
_SONNET_INPUT_PER_M = 3.0
_SONNET_OUTPUT_PER_M = 15.0

# Opus 4 pricing used by reduce
_OPUS_INPUT_PER_M = 15.0
_OPUS_OUTPUT_PER_M = 75.0

# Rough token-per-word ratio for English prose
_TOKENS_PER_WORD = 1.3

# Per-stage input/output overhead in tokens (prompt + system + fixed context)
_MAP_PROMPT_OVERHEAD = 2_000
_MAP_OUTPUT_PER_CHAPTER = 4_000  # structured extraction sidecar
_PLAN_INPUT = 20_000  # whole-book sidecar digest
_PLAN_OUTPUT = 5_000
_REDUCE_INPUT_PER_ARTIFACT = 12_000
_REDUCE_OUTPUT_PER_ARTIFACT = 5_000

# Reduce artifact-count heuristic: roughly one artifact per 2 content
# chapters, plus 1 skill + 3 agents + a handful of commands. Floor at 8.
_ARTIFACT_BASE = 8
_ARTIFACTS_PER_CONTENT_CHAPTER = 0.5

# The point numbers above are the *pessimistic* end — they over-count
# tokens to avoid under-promising the "continue? [y/N]" prompt. The low
# end of the displayed range applies this multiplier to model realistic
# savings: prompt caching on repeated system/tool-use prompts, shorter
# real outputs than the worst-case overhead, and fewer reduce artifacts
# than the upper-bound artifact count. 0.65 is an empirical middle —
# enough savings to be useful, not so aggressive it misleads.
_LOW_END_MULTIPLIER = 0.65


@dataclass(frozen=True)
class StageEstimate:
    stage: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str


@dataclass(frozen=True)
class RunEstimate:
    book_title: str
    content_chapters: int
    total_words: int
    stages: list[StageEstimate] = field(default_factory=list)

    @property
    def total_input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.stages)

    @property
    def total_output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.stages)

    @property
    def total_cost_usd(self) -> float:
        """Pessimistic upper-bound cost — the right number to promise against."""
        return sum(s.cost_usd for s in self.stages)

    @property
    def total_cost_low_usd(self) -> float:
        """Realistic lower-bound cost after cache + output-slack savings."""
        return self.total_cost_usd * _LOW_END_MULTIPLIER

    @property
    def total_calls(self) -> int:
        return sum(s.calls for s in self.stages)


def _sonnet_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * _SONNET_INPUT_PER_M + (
        output_tokens / 1_000_000
    ) * _SONNET_OUTPUT_PER_M


def _opus_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * _OPUS_INPUT_PER_M + (
        output_tokens / 1_000_000
    ) * _OPUS_OUTPUT_PER_M


def estimate_run(
    book: BookManifest,
    chapters: list[NormalizedChapter],
    *,
    include_cleanup: bool = False,
    allowed_ids: set[str] | None = None,
) -> RunEstimate:
    """Predict token counts and cost for a full ``franklin run``.

    Pass ``include_cleanup=True`` when the run will use the Tier 4 LLM
    cleanup pass (``--clean``) — it's a meaningful addition to the total
    and shouldn't be hidden from the pre-run prompt.

    ``allowed_ids`` narrows the estimate to a user-chosen subset of
    chapter ids — used by the pick-flow gate when the user deselects
    chapters they don't want mapped, so the re-displayed cost reflects
    what the run will actually spend.
    """
    toc_kind_by_id = {entry.id: entry.kind for entry in book.structure.toc}
    content_chapters = [
        c for c in chapters if toc_kind_by_id.get(c.chapter_id) == ChapterKind.CONTENT
    ]
    # Fall back to all chapters if the TOC alignment didn't match —
    # better to over-estimate than crash on a partial manifest.
    if not content_chapters:
        content_chapters = list(chapters)
    if allowed_ids is not None:
        content_chapters = [c for c in content_chapters if c.chapter_id in allowed_ids]

    total_words = sum(c.word_count for c in content_chapters)
    stages: list[StageEstimate] = []

    # ---- map stage ----
    map_input_per_chapter = (
        int(_MAP_PROMPT_OVERHEAD)
        + int(max(c.word_count for c in content_chapters) * _TOKENS_PER_WORD)
        if content_chapters
        else _MAP_PROMPT_OVERHEAD
    )
    map_input_total = 0
    for c in content_chapters:
        map_input_total += _MAP_PROMPT_OVERHEAD + int(c.word_count * _TOKENS_PER_WORD)
    map_output_total = len(content_chapters) * _MAP_OUTPUT_PER_CHAPTER
    stages.append(
        StageEstimate(
            stage="map",
            calls=len(content_chapters),
            input_tokens=map_input_total,
            output_tokens=map_output_total,
            cost_usd=_sonnet_cost(map_input_total, map_output_total),
            model="claude-sonnet-4-6",
        )
    )

    # ---- plan stage ----
    stages.append(
        StageEstimate(
            stage="plan",
            calls=1,
            input_tokens=_PLAN_INPUT,
            output_tokens=_PLAN_OUTPUT,
            cost_usd=_sonnet_cost(_PLAN_INPUT, _PLAN_OUTPUT),
            model="claude-sonnet-4-6",
        )
    )

    # ---- reduce stage ----
    estimated_artifacts = max(
        _ARTIFACT_BASE,
        int(_ARTIFACT_BASE + len(content_chapters) * _ARTIFACTS_PER_CONTENT_CHAPTER),
    )
    reduce_input = estimated_artifacts * _REDUCE_INPUT_PER_ARTIFACT
    reduce_output = estimated_artifacts * _REDUCE_OUTPUT_PER_ARTIFACT
    stages.append(
        StageEstimate(
            stage="reduce",
            calls=estimated_artifacts,
            input_tokens=reduce_input,
            output_tokens=reduce_output,
            cost_usd=_opus_cost(reduce_input, reduce_output),
            model="claude-opus-4-6",
        )
    )

    # ---- optional cleanup ----
    if include_cleanup:
        cleanup_input = 0
        cleanup_output = 0
        for c in content_chapters:
            chapter_tokens = int(c.word_count * _TOKENS_PER_WORD) + 500
            cleanup_input += chapter_tokens
            cleanup_output += chapter_tokens  # cleanup returns full text
        stages.append(
            StageEstimate(
                stage="cleanup",
                calls=len(content_chapters),
                input_tokens=cleanup_input,
                output_tokens=cleanup_output,
                cost_usd=_sonnet_cost(cleanup_input, cleanup_output),
                model="claude-sonnet-4-6",
            )
        )

    # Suppress unused-variable lint; map_input_per_chapter is informative
    # but not returned directly — future "largest chapter" display may use it.
    _ = map_input_per_chapter

    return RunEstimate(
        book_title=book.metadata.title,
        content_chapters=len(content_chapters),
        total_words=total_words,
        stages=stages,
    )
