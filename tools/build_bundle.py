#!/usr/bin/env python3
"""Build a deterministic, minimal DecisionBook marketplace ZIP."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
RUNTIME_FILES = sorted(
    [
        "CHANGELOG.md",
        "LICENSE",
        "README.md",
        "__main__.py",
        "core.py",
        "dashboard_manifest.json",
        "decisionbook.py",
        "manifest.json",
        "requirements.txt",
    ]
)
FIXED_TIME = (2020, 1, 1, 0, 0, 0)
SAFE_PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
SAFE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}\Z")


class BundleBuildError(RuntimeError):
    """Raised when a deterministic marketplace bundle cannot be built."""


def manifest_identity(root: Path = ROOT) -> tuple[str, str]:
    """Return the manifest identity used for the artifact filename."""
    manifest_path = root / "manifest.json"
    try:
        manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleBuildError(f"Could not read {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BundleBuildError("manifest.json must contain a JSON object")
    plugin_id = manifest.get("id")
    version = manifest.get("version")
    if not isinstance(plugin_id, str) or not SAFE_PLUGIN_ID.fullmatch(plugin_id):
        raise BundleBuildError("manifest.json has an unsafe or invalid plugin id")
    if not isinstance(version, str) or not SAFE_VERSION.fullmatch(version):
        raise BundleBuildError("manifest.json has an unsafe or invalid version")
    return plugin_id, version


def artifact_filename(root: Path = ROOT) -> str:
    """Derive the canonical artifact filename from manifest.json."""
    plugin_id, version = manifest_identity(root)
    return f"{plugin_id}-{version}.zip"


OUTPUT = DIST / artifact_filename()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(output: Path = OUTPUT) -> Path:
    missing = [name for name in RUNTIME_FILES if not (ROOT / name).is_file()]
    if missing:
        raise BundleBuildError(f"Missing runtime files: {', '.join(missing)}")
    if output.exists() and not output.is_file():
        raise BundleBuildError(f"Bundle output is not a regular file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".zip.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name in RUNTIME_FILES:
                info = zipfile.ZipInfo(name, FIXED_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(
                    info,
                    (ROOT / name).read_bytes(),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        temporary.replace(output)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise BundleBuildError(f"Could not build {output}: {exc}") from exc
    print(f"built {output}")
    print(f"sha256 {sha256(output)}")
    return output


if __name__ == "__main__":
    build()
