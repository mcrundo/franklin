"""Tolerant Pydantic validation for LLM tool-use payloads.

The map and plan stages force Claude through tool-use with strict
``extra="forbid"`` schemas — the outgoing JSON schema carries
``additionalProperties: false`` so the model is nudged to stay
on-contract. But models do occasionally drift:

- **Stray fields** — e.g. generalizing ``source_quote`` from ``Concept``
  onto ``Principle``. Handled by catching ``extra_forbidden`` errors,
  stripping exactly those paths, and retrying.
- **Stringified JSON** — the model returns a JSON string (``"[{...}]"``)
  where a list or dict is expected. Handled by a pre-validation pass
  that detects string values where the schema expects a composite type
  and ``json.loads`` them in place.

Both recovery paths log warnings so the drift is visible in run output
rather than silently swallowed.
"""

from __future__ import annotations

import copy
import json
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
    """Validate ``payload`` against ``model_cls``, recovering from LLM drift.

    ``label`` is included in the warning log line and the eventual error
    message so callers can identify which artifact / chapter the
    payload came from when triaging.
    """
    cleaned = _fix_stringified_json(payload, label)
    try:
        return model_cls.model_validate(cleaned)
    except ValidationError as exc:
        errors = exc.errors()
        extras = [e for e in errors if e.get("type") == "extra_forbidden"]
        non_extras = [e for e in errors if e.get("type") != "extra_forbidden"]
        if not extras or non_extras:
            raise

        cleaned = copy.deepcopy(cleaned) if isinstance(cleaned, dict) else cleaned
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


def _fix_stringified_json(payload: Any, label: str) -> Any:
    """Detect top-level values that are JSON strings and deserialize them.

    LLMs occasionally return ``"[{...}]"`` (a string) where the schema
    expects ``[{...}]`` (an actual list). This walks the top-level dict
    keys and, for any value that's a string starting with ``[`` or ``{``,
    attempts ``json.loads``. On success, replaces in-place and logs.
    Failures are silently ignored — Pydantic will catch the real error.
    """
    if not isinstance(payload, dict):
        return payload
    fixed: list[str] = []
    for key, value in payload.items():
        if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, (list, dict)):
                payload[key] = parsed
                fixed.append(key)
    if fixed:
        logger.warning(
            "%s: deserialized %d stringified JSON field(s): %s",
            label,
            len(fixed),
            ", ".join(fixed),
        )
    return payload


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
