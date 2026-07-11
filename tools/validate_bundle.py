#!/usr/bin/env python3
"""Validate a DecisionBook ZIP without executing or extracting its contents."""

from __future__ import annotations

import argparse
import hashlib
import stat
import zipfile
from pathlib import Path, PurePosixPath

try:  # Support both ``python -m tools.validate_bundle`` and direct execution.
    from .build_bundle import OUTPUT, ROOT, RUNTIME_FILES
except ImportError:  # pragma: no cover - exercised by the release audit subprocess style
    from build_bundle import OUTPUT, ROOT, RUNTIME_FILES


MAX_FILES = 200
MAX_COMPRESSED = 10 * 1024 * 1024
MAX_UNCOMPRESSED = 40 * 1024 * 1024
REQUIRED = {
    "manifest.json",
    "__main__.py",
    "decisionbook.py",
    "core.py",
    "requirements.txt",
}
FORBIDDEN_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "tests",
    "tools",
    "venv",
}
READ_CHUNK = 1024 * 1024


class BundleValidationError(ValueError):
    """Raised when a marketplace artifact violates the release policy."""


def _unsafe_path(name: str) -> bool:
    pure = PurePosixPath(name)
    return (
        not name
        or "\\" in name
        or pure.is_absolute()
        or ".." in pure.parts
        or name.startswith(("/", "\\"))
        or str(pure) != name
    )


def _source_digest(name: str) -> str:
    return hashlib.sha256((ROOT / name).read_bytes()).hexdigest()


def _entry_digest(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "r") as handle:
        while chunk := handle.read(READ_CHUNK):
            size += len(chunk)
            if size > MAX_UNCOMPRESSED:
                raise BundleValidationError(
                    f"entry expands beyond the bundle limit: {info.filename}"
                )
            digest.update(chunk)
    return digest.hexdigest(), size


def _archive_policy_errors(
    infos: list[zipfile.ZipInfo],
    names: list[str],
    unique_names: set[str],
    declared_size: int,
) -> list[str]:
    errors: list[str] = []
    if len(infos) > MAX_FILES:
        errors.append("bundle exceeds 200 files")
    if len(names) != len(unique_names):
        errors.append("bundle contains duplicate entries")
    if unique_names != set(RUNTIME_FILES):
        errors.append(
            f"bundle file allowlist mismatch: {sorted(unique_names ^ set(RUNTIME_FILES))}"
        )
    if not REQUIRED.issubset(unique_names):
        errors.append(f"missing required root files: {sorted(REQUIRED - unique_names)}")
    if declared_size > MAX_UNCOMPRESSED:
        errors.append("bundle exceeds 40 MB uncompressed")
    return errors


def _entry_policy_errors(info: zipfile.ZipInfo) -> list[str]:
    errors: list[str] = []
    name = info.filename
    pure = PurePosixPath(name)
    mode = info.external_attr >> 16

    if _unsafe_path(name):
        errors.append(f"unsafe path: {name}")
    if info.is_dir():
        errors.append(f"directory entry included: {name}")
    if FORBIDDEN_PARTS.intersection(pure.parts) or name.endswith((".pyc", ".pyo")):
        errors.append(f"development artifact included: {name}")
    if info.flag_bits & 0x1:
        errors.append(f"encrypted entry included: {name}")
    if info.compress_type != zipfile.ZIP_DEFLATED:
        errors.append(f"unexpected compression method: {name}")
    if stat.S_ISLNK(mode):
        errors.append(f"symlink included: {name}")
    elif mode and not stat.S_ISREG(mode):
        errors.append(f"non-regular file included: {name}")
    if mode & 0o111:
        errors.append(f"unexpected executable file: {name}")
    return errors


def _can_compare_entry(info: zipfile.ZipInfo, names: list[str], safe_to_read: bool) -> bool:
    mode = info.external_attr >> 16
    return (
        safe_to_read
        and info.filename in RUNTIME_FILES
        and names.count(info.filename) == 1
        and not info.is_dir()
        and not info.flag_bits & 0x1
        and info.compress_type == zipfile.ZIP_DEFLATED
        and not stat.S_ISLNK(mode)
    )


def _source_parity_errors(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> list[str]:
    errors: list[str] = []
    digest, actual_size = _entry_digest(archive, info)
    if actual_size != info.file_size:
        errors.append(f"entry size metadata mismatch: {info.filename}")
    if digest != _source_digest(info.filename):
        errors.append(f"bundle entry differs from source: {info.filename}")
    return errors


def _inspect_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    names: list[str],
    safe_to_read: bool,
) -> list[str]:
    errors = _entry_policy_errors(info)
    if _can_compare_entry(info, names, safe_to_read):
        errors.extend(_source_parity_errors(archive, info))
    return errors


def _inspect_archive(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        unique_names = set(names)
        declared_size = sum(item.file_size for item in infos)
        errors = _archive_policy_errors(infos, names, unique_names, declared_size)
        safe_to_read = declared_size <= MAX_UNCOMPRESSED
        for item in infos:
            errors.extend(_inspect_entry(archive, item, names, safe_to_read))
    return errors


def validate(path: Path) -> None:
    """Validate *path* against platform limits and the exact runtime allowlist."""
    if not path.is_file():
        raise BundleValidationError(f"Bundle does not exist: {path}")

    errors: list[str] = []
    try:
        if path.stat().st_size > MAX_COMPRESSED:
            errors.append("compressed bundle exceeds 10 MB")
        errors.extend(_inspect_archive(path))
    except BundleValidationError as exc:
        errors.append(str(exc))
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        errors.append(f"invalid or unreadable ZIP: {exc}")

    if errors:
        raise BundleValidationError("Bundle validation failed:\n- " + "\n- ".join(errors))
    print(f"bundle valid: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", nargs="?", type=Path, default=OUTPUT)
    args = parser.parse_args()
    try:
        validate(args.bundle.resolve())
    except BundleValidationError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
