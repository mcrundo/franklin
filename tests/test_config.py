"""Tests for root-local Franklin configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from franklin.config import ConfigError, ModelConfig, load_model_config, resolve_model


def test_load_model_config_defaults_when_no_file(tmp_path: Path) -> None:
    config = load_model_config(tmp_path)

    assert config == ModelConfig()
    assert config.plan == "claude-opus-4-8"


def test_load_model_config_reads_models_mapping(tmp_path: Path) -> None:
    (tmp_path / "franklin.yml").write_text(
        """
models:
  map: custom-map
  plan: custom-plan
  reduce: custom-reduce
  cleanup: custom-cleanup
""".lstrip(),
        encoding="utf-8",
    )

    config = load_model_config(tmp_path)

    assert config.map == "custom-map"
    assert config.plan == "custom-plan"
    assert config.reduce == "custom-reduce"
    assert config.cleanup == "custom-cleanup"


def test_resolve_model_lets_explicit_override_win(tmp_path: Path) -> None:
    (tmp_path / "franklin.yml").write_text(
        "models:\n  plan: configured-plan\n",
        encoding="utf-8",
    )

    assert resolve_model("plan", "cli-plan", cwd=tmp_path) == "cli-plan"


def test_load_model_config_rejects_unknown_stage(tmp_path: Path) -> None:
    (tmp_path / "franklin.yml").write_text(
        "models:\n  summarize: nope\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown model stage"):
        load_model_config(tmp_path)
