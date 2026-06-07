"""Unit tests for the DB-free transcript archive sink (sessions/archive.py)."""

from __future__ import annotations

from pathlib import Path

from rekol.sessions.archive import load_manifest, save_manifest


def test_manifest_round_trips(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    manifest = {"proj/sess.jsonl": {"mtime_unix": 100, "size_bytes": 42}}
    save_manifest(archive_dir, manifest)
    assert (archive_dir / ".manifest.json").is_file()
    assert load_manifest(archive_dir) == manifest


def test_load_manifest_absent_returns_empty(tmp_path: Path) -> None:
    assert load_manifest(tmp_path) == {}


def test_load_manifest_corrupt_returns_empty(tmp_path: Path) -> None:
    """A corrupt manifest must NOT crash a sync — it degrades to 'archive
    everything fresh' (copy-if-changed is idempotent), never a traceback."""
    (tmp_path / ".manifest.json").write_text("{ not json")
    assert load_manifest(tmp_path) == {}
