"""Tests for output file management utility."""

import time
from pathlib import Path

from gilbert.core.output import cleanup_old_files, get_output_dir


def test_get_output_dir_creates_directory(tmp_path: Path, monkeypatch: object) -> None:
    import gilbert.core.output as output_mod

    monkeypatch.setattr(output_mod, "OUTPUT_DIR", tmp_path / "output")  # type: ignore[attr-defined]
    result = get_output_dir("tts")
    assert result == tmp_path / "output" / "tts"
    assert result.is_dir()


def test_cleanup_old_files_deletes_expired(tmp_path: Path) -> None:
    old_file = tmp_path / "old.txt"
    old_file.write_text("old")
    # Backdate mtime by 2 hours
    old_time = time.time() - 7200
    import os

    os.utime(old_file, (old_time, old_time))

    new_file = tmp_path / "new.txt"
    new_file.write_text("new")

    deleted = cleanup_old_files(tmp_path, max_age_seconds=3600)
    assert deleted == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_cleanup_old_files_empty_dir(tmp_path: Path) -> None:
    deleted = cleanup_old_files(tmp_path, max_age_seconds=3600)
    assert deleted == 0


def test_cleanup_old_files_nonexistent_dir(tmp_path: Path) -> None:
    deleted = cleanup_old_files(tmp_path / "nope", max_age_seconds=3600)
    assert deleted == 0
