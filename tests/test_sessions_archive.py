"""Unit tests for the DB-free transcript archive sink (sessions/archive.py)."""

from __future__ import annotations

import json
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


def _write_jsonl_with_cwd(p: Path, cwd: str) -> None:
    """Write a one-row transcript whose `cwd` is the REAL project path.

    The exclude matcher runs against this cwd (e.g. `/Users/x/secret-project`),
    NOT the on-disk folder name — so these fixtures deliberately put the matchable
    path in `cwd`, while the slug folder on disk is a distinct, non-matching name,
    proving the match is cwd-driven (the design decision: exclude the real cwd).
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "type": "user",
        "uuid": "u1",
        "sessionId": p.stem,
        "cwd": cwd,
        "message": {"role": "user", "content": "hi"},
    }
    p.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_archive_directory_forward_skips_excluded(tmp_path: Path) -> None:
    live_root = tmp_path / "projects"
    archive_dir = tmp_path / "archive"
    # On-disk slug folders are URL-encoded and do NOT contain a `/secret-project/`
    # segment; the matchable path lives in each row's cwd. The exclude must match
    # the cwd, not the slug.
    _write_jsonl_with_cwd(
        live_root / "-Users-x-secret-project" / "s1.jsonl", "/Users/x/secret-project"
    )
    _write_jsonl_with_cwd(live_root / "-Users-x-public" / "s2.jsonl", "/Users/x/public")

    # Pattern matches the cwd VALUE (which has no trailing slash), so `*/secret-project*`
    # — not `*/secret-project/*`, which would require a path segment AFTER the dir.
    stats = archive_directory(live_root, archive_dir, exclude_patterns=["*/secret-project*"])
    assert stats.files_skipped_excluded == 1
    assert stats.files_copied == 1
    # The excluded project is NEVER archived (matched on its real cwd).
    assert not (archive_dir / "-Users-x-secret-project").exists()
    assert (archive_dir / "-Users-x-public" / "s2.jsonl").exists()


def test_archive_directory_retroactively_removes_now_excluded(tmp_path: Path) -> None:
    """An already-archived file that becomes excluded must be removed on the
    next sync — the safe retroactive flat-file delete in rekol's own folder. The
    match is on the file's real cwd, symmetric with the forward skip."""
    live_root = tmp_path / "projects"
    archive_dir = tmp_path / "archive"
    _write_jsonl_with_cwd(live_root / "-Users-x-clientwork" / "s1.jsonl", "/Users/x/clientwork")
    _write_jsonl_with_cwd(live_root / "-Users-x-public" / "s2.jsonl", "/Users/x/public")

    # First sync with NO excludes archives everything.
    archive_directory(live_root, archive_dir, exclude_patterns=[])
    assert (archive_dir / "-Users-x-clientwork" / "s1.jsonl").exists()

    # User adds an exclude; next sync removes the now-excluded archived copy
    # (matched on the archived file's real cwd, not its slug folder name).
    stats = archive_directory(live_root, archive_dir, exclude_patterns=["*/clientwork*"])
    assert stats.files_removed_excluded == 1
    assert not (archive_dir / "-Users-x-clientwork" / "s1.jsonl").exists()
    assert (archive_dir / "-Users-x-public" / "s2.jsonl").exists()
    # The manifest entry for the removed file is also dropped.
    manifest = load_manifest(archive_dir)
    assert "-Users-x-clientwork/s1.jsonl" not in manifest


def test_archive_directory_matches_cwd_not_slug(tmp_path: Path) -> None:
    """The design decision, asserted directly: a pattern that matches the cwd but
    NOT the URL-encoded slug excludes; a pattern that matches only the slug does
    NOT. The slug `-Users-x-secret` never contains a `/secret/` segment, so a
    natural `*/secret/*` pattern must reach the cwd to work at all."""
    live_root = tmp_path / "projects"
    archive_dir = tmp_path / "archive"
    _write_jsonl_with_cwd(live_root / "-Users-x-secret" / "s.jsonl", "/Users/x/secret")

    # A slug-shaped pattern (matches the on-disk folder, not the cwd) must NOT
    # exclude — we deliberately do not match the slug.
    kept = archive_directory(live_root, archive_dir, exclude_patterns=["*-Users-x-secret*"])
    assert kept.files_skipped_excluded == 0
    assert kept.files_copied == 1

    # A natural cwd-shaped pattern DOES exclude (on the retroactive sweep here).
    excluded = archive_directory(live_root, archive_dir, exclude_patterns=["*/secret*"])
    assert excluded.files_removed_excluded == 1
