"""Unit tests for the DB-free transcript archive sink (sessions/archive.py)."""

from __future__ import annotations

from pathlib import Path

from rekol.sessions.archive import archive_file, load_manifest, save_manifest


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


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_archive_file_copies_when_no_archived_copy(tmp_path: Path) -> None:
    live = tmp_path / "live" / "sess.jsonl"
    archived = tmp_path / "arch" / "sess.jsonl"
    _write(live, "line1\n")
    manifest: dict = {}
    result = archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    assert result.action == "copied"
    assert archived.read_text() == "line1\n"
    assert manifest["sess.jsonl"]["size_bytes"] == len("line1\n")


def test_archive_file_skips_when_unchanged(tmp_path: Path) -> None:
    live = tmp_path / "live" / "sess.jsonl"
    archived = tmp_path / "arch" / "sess.jsonl"
    _write(live, "line1\n")
    manifest: dict = {}
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    # Second call with no change to live → skip (manifest mtime+size match).
    result = archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    assert result.action == "skipped_unchanged"


def test_archive_file_replaces_on_append_prefix(tmp_path: Path) -> None:
    live = tmp_path / "live" / "sess.jsonl"
    archived = tmp_path / "arch" / "sess.jsonl"
    _write(live, "line1\n")
    manifest: dict = {}
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    # Live grows; archived content is a true prefix of the new live → replace.
    _write(live, "line1\nline2\n")
    result = archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    assert result.action == "replaced_append"
    assert archived.read_text() == "line1\nline2\n"


def test_archive_file_sidecars_on_divergence(tmp_path: Path) -> None:
    """Compaction/rewrite signature: live is shorter OR not a prefix of the
    archived copy. We must keep the existing archive AND write a divergence
    sidecar <stem>.<shorthash>.jsonl — never overwrite, never silently lose."""
    live = tmp_path / "live" / "sess.jsonl"
    archived = tmp_path / "arch" / "sess.jsonl"
    _write(live, "line1\nline2\nline3\n")
    manifest: dict = {}
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    # Live is REWRITTEN to a shorter, non-prefix content (compaction).
    _write(live, "rewritten\n")
    result = archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    assert result.action == "diverged_sidecar"
    # Original archive is untouched.
    assert archived.read_text() == "line1\nline2\nline3\n"
    # A sidecar exists alongside it with the new content.
    sidecars = list(archived.parent.glob("sess.*.jsonl"))
    assert len(sidecars) == 1
    assert sidecars[0].read_text() == "rewritten\n"


def test_archive_file_sidecar_is_idempotent(tmp_path: Path) -> None:
    """Re-running a diverged sync must not pile up duplicate sidecars: the
    shorthash is content-derived, so the same divergence yields the same path."""
    live = tmp_path / "live" / "sess.jsonl"
    archived = tmp_path / "arch" / "sess.jsonl"
    _write(live, "original\n")
    manifest: dict = {}
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    _write(live, "rewritten\n")
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    sidecars = list(archived.parent.glob("sess.*.jsonl"))
    assert len(sidecars) == 1  # same content → same shorthash → one sidecar


from rekol.sessions.archive import archive_directory  # noqa: E402


def test_archive_directory_copies_all_then_skips_on_rerun(tmp_path: Path) -> None:
    live_root = tmp_path / "projects"
    archive_dir = tmp_path / "archive"
    _write(live_root / "projA" / "s1.jsonl", "a\n")
    _write(live_root / "projB" / "s2.jsonl", "b\n")

    first = archive_directory(live_root, archive_dir, exclude_patterns=[])
    assert first.files_copied == 2
    assert first.files_skipped_unchanged == 0
    # Mirror layout is preserved under the archive root.
    assert (archive_dir / "projA" / "s1.jsonl").read_text() == "a\n"
    assert (archive_dir / "projB" / "s2.jsonl").read_text() == "b\n"

    # Re-run with nothing changed → all skipped via the manifest.
    second = archive_directory(live_root, archive_dir, exclude_patterns=[])
    assert second.files_copied == 0
    assert second.files_skipped_unchanged == 2


def test_archive_directory_counts_os_errors_without_aborting(tmp_path: Path, monkeypatch) -> None:
    """One unreadable file must not abort the whole sync: it is counted in
    files_errored and the rest still archive."""
    live_root = tmp_path / "projects"
    archive_dir = tmp_path / "archive"
    _write(live_root / "good.jsonl", "ok\n")
    _write(live_root / "bad.jsonl", "boom\n")

    import rekol.sessions.archive as archive_mod

    real_archive_file = archive_mod.archive_file

    def flaky(live_path, archived_path, manifest, *, manifest_key):
        if live_path.name == "bad.jsonl":
            raise OSError("simulated unreadable file")
        return real_archive_file(live_path, archived_path, manifest, manifest_key=manifest_key)

    monkeypatch.setattr(archive_mod, "archive_file", flaky)
    stats = archive_directory(live_root, archive_dir, exclude_patterns=[])
    assert stats.files_copied == 1
    assert stats.files_errored == 1
    assert (archive_dir / "good.jsonl").read_text() == "ok\n"
