"""Root-local Franklin configuration.

Franklin intentionally keeps configuration small: if a ``franklin.yml``
or ``franklin.yaml`` exists in the current working directory, it can
override the Anthropic model used by each paid pipeline stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from franklin.llm.models import CLEANUP_MODEL, MAP_MODEL, PLAN_MODEL, REDUCE_MODEL

CONFIG_FILENAMES = ("franklin.yml", "franklin.yaml")
StageName = Literal["map", "plan", "reduce", "cleanup"]


class ConfigError(ValueError):
    """Raised when a root-local Franklin config file is present but invalid."""


@dataclass(frozen=True)
class ModelConfig:
    map: str = MAP_MODEL
    plan: str = PLAN_MODEL
    reduce: str = REDUCE_MODEL
    cleanup: str = CLEANUP_MODEL

    def model_for(self, stage: StageName) -> str:
        if stage == "map":
            return self.map
        if stage == "plan":
            return self.plan
        if stage == "reduce":
            return self.reduce
        return self.cleanup


def find_config_file(cwd: Path | None = None) -> Path | None:
    """Return the first supported config file in ``cwd`` if one exists."""
    root = cwd or Path.cwd()
    for filename in CONFIG_FILENAMES:
        candidate = root / filename
        if candidate.exists():
            return candidate
    return None


def load_model_config(cwd: Path | None = None) -> ModelConfig:
    """Load model settings from ``franklin.yml`` or ``franklin.yaml``.

    Expected shape:

    ```yaml
    models:
      map: claude-sonnet-4-6
      plan: claude-opus-4-8
      reduce: claude-sonnet-4-6
      cleanup: claude-sonnet-4-6
    ```
    """
    path = find_config_file(cwd)
    if path is None:
        return ModelConfig()

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return ModelConfig()
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path.name} must contain a YAML mapping")

    models = loaded.get("models", {})
    if models is None:
        return ModelConfig()
    if not isinstance(models, dict):
        raise ConfigError(f"{path.name}: models must be a mapping")

    allowed = {"map", "plan", "reduce", "cleanup"}
    unknown = sorted(str(key) for key in models if str(key) not in allowed)
    if unknown:
        raise ConfigError(f"{path.name}: unknown model stage(s): {', '.join(unknown)}")

    values: dict[str, str] = {}
    for key, value in models.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{path.name}: models.{key} must be a non-empty string")
        values[str(key)] = value.strip()

    return ModelConfig(**values)


def resolve_model(stage: StageName, override: str | None = None, cwd: Path | None = None) -> str:
    """Resolve the model for ``stage``, letting an explicit CLI override win."""
    if override:
        return override
    return load_model_config(cwd).model_for(stage)


__all__ = [
    "CONFIG_FILENAMES",
    "ConfigError",
    "ModelConfig",
    "StageName",
    "find_config_file",
    "load_model_config",
    "resolve_model",
]
