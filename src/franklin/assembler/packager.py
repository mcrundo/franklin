"""Package a generated plugin tree as a distributable zip archive."""

from __future__ import annotations

import zipfile
from pathlib import Path


def package_plugin(plugin_root: Path, output_path: Path) -> Path:
    """Write a zip archive containing the plugin tree.

    Archive entries are prefixed with the plugin directory name so an
    unzip yields a single top-level directory matching `plugin_root.name`.
    This matches the convention for distributable Claude Code plugins.

    Returns the path the archive was written to.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    archive_base = plugin_root.parent

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(plugin_root.rglob("*")):
            if not file_path.is_file():
                continue
            arcname = file_path.relative_to(archive_base)
            archive.write(file_path, arcname.as_posix())

    return output_path
