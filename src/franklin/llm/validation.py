"""Tolerant Pydantic validation for LLM tool-use payloads.

The map and plan stages force Claude through tool-use with strict
``extra="forbid"`` schemas — the outgoing JSON schema carries
``additionalProperties: false`` so the model is nudged to stay
on-contract. But models do occasionally slip an unknown key onto a
sub-object (e.g. generalizing ``source_quote`` from ``Concept`` onto
``Principle``), and a single stray field shouldn't kill an entire
chapter's worth of extraction or a whole plan call.

This helper validates strictly first, and if the *only* failures are
``extra_forbidden`` errors, strips exactly those paths from a deep copy
of the payload, logs a warning naming what was dropped, and retries.
Any other validation error (missing required field, wrong type) still
raises — we don't mask real bugs.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


def validate_with_extra_recovery[ModelT: BaseModel](
    model_cls: type[ModelT],
    payload: Any,
    *,
    label: str,
) -> ModelT:
    """Validate ``payload`` against ``model_cls``, recovering from stray extras.

    ``label`` is included in the warning log line and the eventual error
    message so callers can identify which artifact / chapter the
    payload came from when triaging.
    """
    try:
        return model_cls.model_validate(payload)
    except ValidationError as exc:
        errors = exc.errors()
        extras = [e for e in errors if e.get("type") == "extra_forbidden"]
        non_extras = [e for e in errors if e.get("type") != "extra_forbidden"]
        if not extras or non_extras:
            raise

        cleaned = copy.deepcopy(payload) if isinstance(payload, dict) else payload
        stripped_paths: list[str] = []
        for err in extras:
            loc = err.get("loc", ())
            if _delete_at_path(cleaned, loc):
                stripped_paths.append(".".join(str(p) for p in loc))

        if stripped_paths:
            logger.warning(
                "%s: stripped %d stray field(s): %s",
                label,
                len(stripped_paths),
                ", ".join(stripped_paths),
            )

        return model_cls.model_validate(cleaned)


def _delete_at_path(payload: Any, loc: tuple[Any, ...] | list[Any]) -> bool:
    """Delete the key identified by a Pydantic error ``loc`` path.

    Pydantic reports ``loc`` as a tuple of dict keys and list indices,
    e.g. ``("principles", 2, "source_quote")``. We walk to the parent of
    the final segment and delete the key there. Returns True when a key
    was actually removed, False if the path didn't resolve (e.g. because
    a prior fix already dropped a parent).
    """
    if not loc:
        return False
    parent: Any = payload
    for part in loc[:-1]:
        try:
            parent = parent[part]
        except (KeyError, IndexError, TypeError):
            return False
    last = loc[-1]
    if isinstance(parent, dict) and last in parent:
        del parent[last]
        return True
    return False
