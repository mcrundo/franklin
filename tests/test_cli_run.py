"""Tests for `franklin run` and its --push integration.

Covers the flag-validation branches (missing --repo, push flags without
--push), the successful --push chain (with license seeded so the gate
passes), and the license-gate-fires-after-assembly branch. The
underlying stage functions are mocked so the tests don't touch the
Anthropic API, git, or the real filesystem.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
import typer

from franklin import license as license_mod
from franklin.cli import run_pipeline
from franklin.license import _BYPASS_ENV_VAR

_FIXTURE_PRIVATE_KEY = (
    Path(__file__).parent / "fixtures" / "license_private_key.pem"
).read_bytes()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mint(features: list[str] | None = None) -> str:
    iat = datetime.now(tz=UTC)
    exp = iat + timedelta(days=365)
    claims: dict[str, Any] = {
        "sub": "user@example.com",
        "iat": int(iat.timestamp()),
        "exp": int(exp.timestamp()),
        "features": features if features is not None else ["push", "install"],
        "plan": "pro",
        "jti": "run-push-test",
    }
    return jwt.encode(claims, _FIXTURE_PRIVATE_KEY, algorithm="RS256")


def _seed_license(token: str) -> None:
    license_mod._save_license_token(token)
    state = license_mod._LocalState(last_online_at=datetime.now(tz=UTC))
    license_mod._save_state(state)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRANKLIN_LICENSE_DIR", str(tmp_path / "franklin-cfg"))
    monkeypatch.delenv(_BYPASS_ENV_VAR, raising=False)
    monkeypatch.setattr(license_mod, "_refresh_revocations_opportunistic", lambda state: False)


@pytest.fixture
def book_epub(tmp_path: Path) -> Path:
    """A stand-in .epub file — run_pipeline's typer.Argument only checks it exists."""
    path = tmp_path / "book.epub"
    path.write_bytes(b"not a real epub")
    return path


@pytest.fixture
def stage_mocks() -> dict[str, MagicMock]:
    """Mock every pipeline stage so run_pipeline is a pure orchestrator test."""
    return {
        "ingest": MagicMock(),
        "map": MagicMock(),
        "plan": MagicMock(),
        "reduce": MagicMock(),
        "assemble": MagicMock(),
        # push stage mocks push_command (the CLI stage), not push_plugin —
        # the stages list in run_pipeline calls push_command by name.
        "push": MagicMock(),
    }


def _patch_stages(stage_mocks: dict[str, MagicMock]) -> Any:
    """Return a single context manager that patches every stage function."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("franklin.cli.ingest", stage_mocks["ingest"]))
    stack.enter_context(patch("franklin.cli.map_chapters", stage_mocks["map"]))
    stack.enter_context(patch("franklin.cli.plan_pipeline", stage_mocks["plan"]))
    stack.enter_context(patch("franklin.cli.reduce_pipeline", stage_mocks["reduce"]))
    stack.enter_context(patch("franklin.cli.assemble_pipeline", stage_mocks["assemble"]))
    stack.enter_context(patch("franklin.cli.push_command", stage_mocks["push"]))
    return stack


def _write_assembled_run(run_dir: Path, plugin_name: str = "test-plugin") -> None:
    """Write the minimal files push_command needs to reach the license gate.

    Used by the gate-failure test where we want push_command to run for
    real (so its internal gate fires) rather than being mocked away.
    """
    from franklin.checkpoint import RunDirectory
    from franklin.schema import (
        BookManifest,
        BookMetadata,
        BookSource,
        BookStructure,
        PlanManifest,
        PluginMeta,
    )

    run_dir.mkdir(parents=True, exist_ok=True)
    rd = RunDirectory(run_dir)
    rd.ensure()
    rd.save_book(
        BookManifest(
            franklin_version="0.1.0",
            source=BookSource(
                path="x.epub",
                sha256="0" * 64,
                format="epub",
                ingested_at=datetime.now(UTC),
            ),
            metadata=BookMetadata(title="Test", authors=["Ada"]),
            structure=BookStructure(),
        )
    )
    rd.save_plan(
        PlanManifest(
            book_id="test",
            generated_at=datetime.now(UTC),
            planner_model="claude-opus-4-6",
            planner_rationale="test",
            plugin=PluginMeta(name=plugin_name, version="0.1.0", description="Test"),
        )
    )
    plugin_dir = rd.output_dir / plugin_name / ".claude-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        '{"name":"' + plugin_name + '","version":"0.1.0","description":"Test"}'
    )


# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------


def test_run_rejects_push_without_repo(
    book_epub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        run_pipeline(
            book_path=book_epub,
            output=None,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=True,
            repo=None,
            branch="main",
            create_pr=False,
            public=False,
        )
    assert exc_info.value.exit_code == 2
    captured = capsys.readouterr()
    assert "--push requires --repo" in captured.out


def test_run_rejects_repo_without_push(
    book_epub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        run_pipeline(
            book_path=book_epub,
            output=None,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=False,
            repo="owner/name",
            branch="main",
            create_pr=False,
            public=False,
        )
    assert exc_info.value.exit_code == 2
    captured = capsys.readouterr()
    assert "--repo" in captured.out
    assert "only be used with --push" in captured.out


def test_run_rejects_pr_without_push(book_epub: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit):
        run_pipeline(
            book_path=book_epub,
            output=None,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=False,
            repo=None,
            branch="main",
            create_pr=True,
            public=False,
        )
    captured = capsys.readouterr()
    assert "--pr" in captured.out


def test_run_rejects_public_without_push(
    book_epub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit):
        run_pipeline(
            book_path=book_epub,
            output=None,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=False,
            repo=None,
            branch="main",
            create_pr=False,
            public=True,
        )
    captured = capsys.readouterr()
    assert "--public" in captured.out


def test_run_rejects_branch_without_push(
    book_epub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit):
        run_pipeline(
            book_path=book_epub,
            output=None,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=False,
            repo=None,
            branch="feature-x",
            create_pr=False,
            public=False,
        )
    captured = capsys.readouterr()
    assert "--branch" in captured.out


# ---------------------------------------------------------------------------
# Successful --push chain
# ---------------------------------------------------------------------------


def test_run_with_push_chains_all_stages(
    book_epub: Path,
    tmp_path: Path,
    stage_mocks: dict[str, MagicMock],
) -> None:
    _seed_license(_mint(features=["push"]))

    run_dir = tmp_path / "run-output"
    with _patch_stages(stage_mocks):
        run_pipeline(
            book_path=book_epub,
            output=run_dir,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=True,
            repo="owner/name",
            branch="main",
            create_pr=False,
            public=False,
        )

    for name in ("ingest", "map", "plan", "reduce", "assemble", "push"):
        assert stage_mocks[name].called, f"{name} stage was not called"

    push_kwargs = stage_mocks["push"].call_args.kwargs
    assert push_kwargs["repo"] == "owner/name"
    assert push_kwargs["branch"] == "main"
    assert push_kwargs["create_pr"] is False
    assert push_kwargs["public"] is False


def test_run_without_push_does_not_invoke_push(
    book_epub: Path,
    tmp_path: Path,
    stage_mocks: dict[str, MagicMock],
) -> None:
    run_dir = tmp_path / "run-output"
    with _patch_stages(stage_mocks):
        run_pipeline(
            book_path=book_epub,
            output=run_dir,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=False,
            repo=None,
            branch="main",
            create_pr=False,
            public=False,
        )

    for name in ("ingest", "map", "plan", "reduce", "assemble"):
        assert stage_mocks[name].called
    assert not stage_mocks["push"].called


def test_run_with_push_propagates_branch_and_pr_and_public(
    book_epub: Path,
    tmp_path: Path,
    stage_mocks: dict[str, MagicMock],
) -> None:
    _seed_license(_mint(features=["push"]))

    run_dir = tmp_path / "run-output"
    with _patch_stages(stage_mocks):
        run_pipeline(
            book_path=book_epub,
            output=run_dir,
            force=False,
            yes=False,
            estimate=False,
            clean=False,
            push=True,
            repo="owner/name",
            branch="franklin/update",
            create_pr=True,
            public=True,
        )

    push_kwargs = stage_mocks["push"].call_args.kwargs
    assert push_kwargs["branch"] == "franklin/update"
    assert push_kwargs["create_pr"] is True
    assert push_kwargs["public"] is True


# ---------------------------------------------------------------------------
# License gate fires on push after successful assembly
# ---------------------------------------------------------------------------


def test_run_push_surfaces_license_gate_error_after_assembly(
    book_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without a license, the run reaches the push stage and the real
    push_command's internal gate fires with the friendly multi-line
    error. push_plugin is mocked so we can assert the underlying push
    was never actually invoked — the gate blocked it before any git
    or GitHub work happened.

    Pre-push stages are mocked (no assertion on which of them ran;
    run_pipeline's resume-on-disk logic correctly skips stages whose
    outputs already exist, and the fixture pre-populates plan.json).
    """
    run_dir = tmp_path / "run-output"
    _write_assembled_run(run_dir, plugin_name="test-plugin")

    fake_push_plugin = MagicMock()

    with (
        patch("franklin.cli.ingest", MagicMock()),
        patch("franklin.cli.map_chapters", MagicMock()),
        patch("franklin.cli.plan_pipeline", MagicMock()),
        patch("franklin.cli.reduce_pipeline", MagicMock()),
        patch("franklin.cli.assemble_pipeline", MagicMock()),
        patch("franklin.cli.push_plugin", fake_push_plugin),
        pytest.raises(typer.Exit),
    ):
        run_pipeline(
            book_path=book_epub,
            output=run_dir,
            force=False,
            yes=True,
            estimate=False,
            clean=False,
            push=True,
            repo="owner/name",
            branch="main",
            create_pr=False,
            public=False,
        )

    # The gate blocked the push before any real publishing work happened.
    assert not fake_push_plugin.called

    captured = capsys.readouterr()
    assert "franklin push is a Pro feature" in captured.out
    assert "no franklin license installed" in captured.out
