"""Pure onboarding detection: transcript discovery and cloud-sync candidates."""

from __future__ import annotations

from pathlib import Path

from rekol.onboarding.detect import CloudSyncDir, count_claude_transcripts, detect_cloud_sync_dirs


def test_count_transcripts_counts_jsonl_recursively(tmp_path: Path) -> None:
    (tmp_path / "projA").mkdir()
    (tmp_path / "projB" / "sub").mkdir(parents=True)
    (tmp_path / "projA" / "a.jsonl").write_text("{}", encoding="utf-8")
    (tmp_path / "projB" / "sub" / "b.jsonl").write_text("{}", encoding="utf-8")
    (tmp_path / "projB" / "notes.md").write_text("x", encoding="utf-8")
    assert count_claude_transcripts(tmp_path) == 2


def test_count_transcripts_missing_dir_is_zero(tmp_path: Path) -> None:
    assert count_claude_transcripts(tmp_path / "nope") == 0


def test_count_transcripts_path_is_file_returns_zero(tmp_path: Path) -> None:
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x", encoding="utf-8")
    assert count_claude_transcripts(f) == 0


def test_detect_cloud_sync_finds_existing_dirs(tmp_path: Path) -> None:
    dropbox = tmp_path / "Dropbox"
    dropbox.mkdir()
    candidates = {
        "Dropbox": dropbox,
        "iCloud Drive": tmp_path / "Library" / "Mobile Documents",  # absent
    }
    found = detect_cloud_sync_dirs(candidates)
    assert found == [CloudSyncDir(label="Dropbox", path=dropbox)]
