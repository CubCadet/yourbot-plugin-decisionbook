from __future__ import annotations

import hashlib
import json
import stat
import warnings
import zipfile
from pathlib import Path

import pytest

from tools import validate_bundle as bundle_validator
from tools.build_bundle import (
    OUTPUT,
    ROOT,
    RUNTIME_FILES,
    BundleBuildError,
    artifact_filename,
    build,
)
from tools.validate_bundle import BundleValidationError, validate

FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_entries() -> list[tuple[str, bytes, int, int]]:
    mode = stat.S_IFREG | 0o644
    return [
        (name, (ROOT / name).read_bytes(), mode, zipfile.ZIP_DEFLATED) for name in RUNTIME_FILES
    ]


def write_archive(
    path: Path,
    entries: list[tuple[str, bytes, int, int]],
) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload, mode, compression in entries:
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = compression
            archive.writestr(info, payload)
    return path


def assert_invalid(path: Path, reason: str) -> None:
    with pytest.raises(BundleValidationError, match=reason):
        validate(path)


def test_artifact_filename_is_derived_from_manifest():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    expected = f"{manifest['id']}-{manifest['version']}.zip"
    assert artifact_filename() == expected
    assert OUTPUT.name == expected


def test_artifact_filename_tracks_manifest_changes(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"id": "decisionbook", "version": "1.2.3-beta.1"}),
        encoding="utf-8",
    )
    assert artifact_filename(tmp_path) == "decisionbook-1.2.3-beta.1.zip"


@pytest.mark.parametrize(
    "manifest",
    [
        {"id": "../escape", "version": "1.0.0"},
        {"id": "decisionbook", "version": "../1.0.0"},
        {"id": "Decision Book", "version": "1.0.0"},
    ],
)
def test_artifact_filename_rejects_unsafe_manifest_identity(tmp_path, manifest):
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleBuildError):
        artifact_filename(tmp_path)


def test_bundle_is_deterministic_valid_and_does_not_touch_release_artifact(tmp_path):
    output = tmp_path / artifact_filename()
    release_before = digest(OUTPUT) if OUTPUT.exists() else None

    build(output)
    first = digest(output)
    build(output)

    assert digest(output) == first
    assert (digest(OUTPUT) if OUTPUT.exists() else None) == release_before
    validate(output)


def test_bundle_has_exact_source_content_and_deterministic_metadata(tmp_path):
    output = build(tmp_path / artifact_filename())
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == RUNTIME_FILES
        for info in archive.infolist():
            assert archive.read(info) == (ROOT / info.filename).read_bytes()
            assert info.date_time == FIXED_TIME
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert info.external_attr >> 16 == stat.S_IFREG | 0o644


def test_failed_build_removes_partial_temporary_file(tmp_path, monkeypatch):
    output = tmp_path / artifact_filename()

    def fail_write(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_write)
    with pytest.raises(BundleBuildError, match="simulated write failure"):
        build(output)
    assert not output.exists()
    assert not output.with_suffix(".zip.tmp").exists()


@pytest.mark.parametrize("unsafe_name", ["../core.py", "/core.py", "..\\core.py"])
def test_validator_rejects_traversal_and_absolute_paths(tmp_path, unsafe_name):
    entries = [
        (unsafe_name if name == "core.py" else name, payload, mode, compression)
        for name, payload, mode, compression in runtime_entries()
    ]
    assert_invalid(write_archive(tmp_path / "unsafe.zip", entries), "unsafe path")


def test_validator_rejects_duplicate_entries(tmp_path):
    entries = runtime_entries()
    entries.append(next(item for item in entries if item[0] == "manifest.json"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        path = write_archive(tmp_path / "duplicate.zip", entries)
    assert_invalid(path, "duplicate entries")


def test_validator_rejects_symlinks(tmp_path):
    entries = [
        (name, payload, stat.S_IFLNK | 0o777, compression)
        if name == "core.py"
        else (name, payload, mode, compression)
        for name, payload, mode, compression in runtime_entries()
    ]
    assert_invalid(write_archive(tmp_path / "symlink.zip", entries), "symlink included")


def test_validator_rejects_executable_files(tmp_path):
    entries = [
        (name, payload, stat.S_IFREG | 0o755, compression)
        if name == "core.py"
        else (name, payload, mode, compression)
        for name, payload, mode, compression in runtime_entries()
    ]
    assert_invalid(write_archive(tmp_path / "executable.zip", entries), "unexpected executable")


@pytest.mark.parametrize(
    ("constant", "reason"),
    [
        ("MAX_COMPRESSED", "compressed bundle exceeds"),
        ("MAX_UNCOMPRESSED", "uncompressed"),
    ],
)
def test_validator_rejects_oversized_archives(tmp_path, monkeypatch, constant, reason):
    path = write_archive(tmp_path / "large.zip", runtime_entries())
    monkeypatch.setattr(bundle_validator, constant, 1)
    assert_invalid(path, reason)


def test_validator_rejects_too_many_files(tmp_path, monkeypatch):
    path = write_archive(tmp_path / "many.zip", runtime_entries())
    monkeypatch.setattr(bundle_validator, "MAX_FILES", 1)
    assert_invalid(path, "exceeds 200 files")


def test_validator_rejects_missing_required_and_allowlisted_file(tmp_path):
    entries = [item for item in runtime_entries() if item[0] != "core.py"]
    path = write_archive(tmp_path / "missing.zip", entries)
    assert_invalid(path, "missing required root files")


def test_validator_rejects_extra_file(tmp_path):
    entries = [
        *runtime_entries(),
        ("extra.py", b"pass\n", stat.S_IFREG | 0o644, zipfile.ZIP_DEFLATED),
    ]
    assert_invalid(write_archive(tmp_path / "extra.zip", entries), "allowlist mismatch")


def test_validator_rejects_development_artifacts(tmp_path):
    entries = [
        *runtime_entries(),
        ("tests/debug.py", b"pass\n", stat.S_IFREG | 0o644, zipfile.ZIP_DEFLATED),
    ]
    assert_invalid(write_archive(tmp_path / "development.zip", entries), "development artifact")


def test_validator_rejects_non_deflate_compression(tmp_path):
    entries = [
        (name, payload, mode, zipfile.ZIP_STORED)
        if name == "core.py"
        else (name, payload, mode, compression)
        for name, payload, mode, compression in runtime_entries()
    ]
    assert_invalid(write_archive(tmp_path / "stored.zip", entries), "unexpected compression method")


def test_validator_rejects_stale_source_content(tmp_path):
    entries = [
        (name, b"tampered\n", mode, compression)
        if name == "core.py"
        else (name, payload, mode, compression)
        for name, payload, mode, compression in runtime_entries()
    ]
    assert_invalid(write_archive(tmp_path / "stale.zip", entries), "differs from source")


def test_validator_rejects_non_zip_input(tmp_path):
    path = tmp_path / "not-a-zip.zip"
    path.write_bytes(b"not a zip")
    assert_invalid(path, "invalid or unreadable ZIP")
