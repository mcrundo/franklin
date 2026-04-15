"""Central registry of default Anthropic model IDs per stage.

Stage packages (``mapper``, ``planner``, ``reducer``, ``ingest.cleanup``)
re-export the matching constant as their ``DEFAULT_MODEL`` so callers
that import from those packages keep working. Bumping a model for a
stage is a one-line change here.

Keep these aligned with the model IDs Anthropic publishes in the
``claude-*`` families. The current choices:

- **Map** (Sonnet): fast, cheap per-chapter extraction. Each chapter
  is independent and Sonnet's output is strict-schema-compliant.
- **Plan** (Opus): the most reasoning-heavy step in the pipeline. The
  planner reads every sidecar and proposes plugin architecture;
  Opus's better synthesis is worth the cost for this one call.
- **Reduce** (Sonnet): per-artifact generation with heavy prompt
  caching. Sonnet handles long, templated content well.
- **Cleanup** (Sonnet): mechanical de-hyphenation and layout repair
  on PDF chapters. No reasoning needed, just careful rewriting.
"""

from __future__ import annotations

MAP_MODEL = "claude-sonnet-4-6"
PLAN_MODEL = "claude-opus-4-6"
REDUCE_MODEL = "claude-sonnet-4-6"
CLEANUP_MODEL = "claude-sonnet-4-6"


__all__ = ["CLEANUP_MODEL", "MAP_MODEL", "PLAN_MODEL", "REDUCE_MODEL"]
