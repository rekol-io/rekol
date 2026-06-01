"""Pure onboarding detection: transcript discovery and cloud-sync candidates."""

from __future__ import annotations

from pathlib import Path

from rekol.onboarding.detect import (
    CloudSyncDir,
    count_claude_transcripts,
    count_curated_memory_files,
    detect_cloud_sync_dirs,
)


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


def test_count_curated_memory_files_counts_layer_markdown(tmp_path: Path) -> None:
    (tmp_path / "always").mkdir()
    (tmp_path / "topics").mkdir()
    (tmp_path / "always" / "identity.md").write_text("x", encoding="utf-8")
    (tmp_path / "topics" / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "topics" / "b.md").write_text("x", encoding="utf-8")
    assert count_curated_memory_files(tmp_path) == 3


def test_count_curated_memory_files_ignores_index_and_top_level(tmp_path: Path) -> None:
    # The always-on index file and machine-only dirs are not user content and
    # must not count toward emptiness (a freshly-seeded store ships REKOL.md).
    (tmp_path / "REKOL.md").write_text("index", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("legacy index", encoding="utf-8")
    (tmp_path / ".index").mkdir()
    (tmp_path / ".index" / "INDEX.md").write_text("auto", encoding="utf-8")
    (tmp_path / ".install-logs").mkdir()
    (tmp_path / ".install-logs" / "note.md").write_text("log", encoding="utf-8")
    assert count_curated_memory_files(tmp_path) == 0


def test_count_curated_memory_files_missing_dir_is_zero(tmp_path: Path) -> None:
    assert count_curated_memory_files(tmp_path / "nope") == 0


def test_count_curated_memory_files_ignores_non_markdown(tmp_path: Path) -> None:
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "topics" / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "rekol.config.yaml").write_text("k: v", encoding="utf-8")
    assert count_curated_memory_files(tmp_path) == 1


def test_detect_cloud_sync_finds_existing_dirs(tmp_path: Path) -> None:
    dropbox = tmp_path / "Dropbox"
    dropbox.mkdir()
    candidates = {
        "Dropbox": dropbox,
        "iCloud Drive": tmp_path / "Library" / "Mobile Documents",  # absent
    }
    found = detect_cloud_sync_dirs(candidates)
    assert found == [CloudSyncDir(label="Dropbox", path=dropbox)]
