"""CLI command submodules.

Each module in this package registers its Typer commands on the shared
``app`` / ``license_app`` / ``runs_app`` instances exposed by
``franklin.cli``. The top-level ``cli.py`` imports this package at the
end of its module body so command registration happens exactly once
and after the Typer apps exist.

Submodule layout:

- :mod:`.diagnostics` — ``doctor``, ``stats``, ``costs``, ``runs list``,
  ``license login/logout/whoami/status``.
"""

from franklin.commands import diagnostics, publishing

__all__ = ["diagnostics", "publishing"]
