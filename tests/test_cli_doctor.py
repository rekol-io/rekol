"""Tests for `rekol doctor` (C5 observability).

Sandbox-only: every test uses a throwaway ``REKOL_HOME`` and the autouse
``_isolate_index_cache`` fixture (conftest) points the cache at a temp dir, so
the real ``~/.cache`` / index is never touched.

The bar (from the C5 spec):
  * a good index reports healthy and exits 0;
  * a corrupt/empty/missing index exits 1 with an actionable remedy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from cache_helpers import cache_dir_for
from rekol.cli_doctor import Status, run_doctor
from rekol.cli_doctor import main as doctor_main
from rekol.cli_session_index import main as session_index_main
from rekol.config import load_config
from rekol.embeddings import HashingEmbedder
from rekol.indexer import Indexer
from rekol.sessions.store import SessionStore
from rekol.store import IndexStore


def _write_memory_home(tmp_path: Path, monkeypatch, *, with_transcripts: bool = True) -> Path:
    """Create a sandboxed REKOL_HOME with a couple of memory files and config."""
    home = tmp_path / "home"
    (home / "topics").mkdir(parents=True)
    (home / "topics" / "prometheus.md").write_text(
        "---\nname: Prometheus\ndescription: metrics\ntype: topic\n"
        "tags: ['prometheus']\naliases: ['prom']\n---\n\n# Prometheus\n\nURL in IaC repo.\n"
    )
    (home / "always").mkdir(parents=True)
    (home / "always" / "identity.md").write_text(
        "---\nname: Identity\ndescription: who\ntype: always\n"
        "tags: ['identity']\naliases: ['me']\n---\n\n# Identity\n\nAlex is an engineer.\n"
    )

    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    if with_transcripts:
        (projects / "session.jsonl").write_text(
            '{"parentUuid":null,"type":"user","message":{"role":"user",'
            '"content":"how do I uninstall the rekol thing"},'
            '"uuid":"u-1","timestamp":"2026-04-24T01:29:01.303Z",'
            '"sessionId":"s-1","cwd":"/tmp/repoA"}\n'
        )

    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\nclaude_projects_dir: {projects.parent}\n"
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    return home


def _build_curated_index(home: Path) -> None:
    """Build a real, healthy curated index for the sandboxed home."""
    cfg = load_config()
    emb = HashingEmbedder(dim=384)
    store = IndexStore(
        db_path=cfg.index_db_path, dim=emb.dim, use_sqlite_vec=False, embedding_model="test-hashing"
    )
    store.init_schema()
    Indexer(
        memory_root=cfg.memory_home, store=store, embedder=emb, index_dir=cfg.index_dir
    ).rebuild()
    store.close()


# ------------------------------- healthy path -------------------------------


def test_doctor_reports_healthy_and_exits_zero(tmp_path: Path, monkeypatch) -> None:
    home = _write_memory_home(tmp_path, monkeypatch)
    _build_curated_index(home)
    runner = CliRunner()
    assert runner.invoke(session_index_main, ["--full"]).exit_code == 0

    result = runner.invoke(doctor_main, [])
    assert result.exit_code == 0, result.output
    assert "index is healthy." in result.output
    # The cache location is always reported.
    assert str(cache_dir_for(home)) in result.output
    # No problem remedies surfaced.
    assert "DEGRADED" not in result.output


def test_run_doctor_all_ok_when_healthy(tmp_path: Path, monkeypatch) -> None:
    """The pure entrypoint: a healthy build yields no PROBLEM findings."""
    home = _write_memory_home(tmp_path, monkeypatch)
    _build_curated_index(home)
    CliRunner().invoke(session_index_main, ["--full"])

    cfg = load_config()
    report = run_doctor(cfg, HashingEmbedder(dim=384))
    assert report.is_healthy is True
    labels = {f.label for f in report.findings if f.status is Status.PROBLEM}
    assert labels == set()
    # The expected checks are all present.
    all_labels = {f.label for f in report.findings}
    assert {"curated schema", "model identity", "curated content", "session FTS"} <= all_labels


# ------------------------------ degraded paths ------------------------------


def test_doctor_exits_one_when_curated_index_missing(tmp_path: Path, monkeypatch) -> None:
    """No curated index built at all → exit 1 with the rebuild remedy."""
    _write_memory_home(tmp_path, monkeypatch)
    # Deliberately do NOT build the curated index.
    result = CliRunner().invoke(doctor_main, [])
    assert result.exit_code == 1, result.output
    assert "DEGRADED" in result.output
    assert "rekol index rebuild" in result.output


def test_doctor_exits_one_on_empty_index(tmp_path: Path, monkeypatch) -> None:
    """A curated index that exists but holds 0 files → exit 1 with remedy."""
    _write_memory_home(tmp_path, monkeypatch)
    cfg = load_config()
    # Build an empty, schema-correct, identity-stamped index (no files indexed).
    store = IndexStore(
        db_path=cfg.index_db_path, dim=384, use_sqlite_vec=False, embedding_model="test-hashing"
    )
    store.init_schema()
    store.close()

    result = CliRunner().invoke(doctor_main, [])
    assert result.exit_code == 1, result.output
    assert "empty" in result.output
    assert "rekol index rebuild" in result.output


def test_doctor_flags_model_identity_mismatch(tmp_path: Path, monkeypatch) -> None:
    """An index built by a different embedding model → PROBLEM + remedy, exit 1."""
    _write_memory_home(tmp_path, monkeypatch)
    cfg = load_config()
    emb = HashingEmbedder(dim=384)
    # Build the index stamped with a DIFFERENT model than the config (test-hashing).
    store = IndexStore(
        db_path=cfg.index_db_path, dim=emb.dim, use_sqlite_vec=False, embedding_model="other-model"
    )
    store.init_schema()
    Indexer(
        memory_root=cfg.memory_home, store=store, embedder=emb, index_dir=cfg.index_dir
    ).rebuild()
    store.close()

    result = CliRunner().invoke(doctor_main, [])
    assert result.exit_code == 1, result.output
    assert "model identity" in result.output
    assert "rekol index rebuild" in result.output


def test_doctor_flags_stale_schema(tmp_path: Path, monkeypatch) -> None:
    """An index with an out-of-date user_version → schema PROBLEM, exit 1."""
    _write_memory_home(tmp_path, monkeypatch)
    cfg = load_config()
    store = IndexStore(
        db_path=cfg.index_db_path, dim=384, use_sqlite_vec=False, embedding_model="test-hashing"
    )
    store.init_schema()
    # Simulate an older index: stamp a lower user_version.
    store.conn.execute("PRAGMA user_version = 2")
    store.conn.commit()
    store.close()

    result = CliRunner().invoke(doctor_main, [])
    assert result.exit_code == 1, result.output
    assert "out of date" in result.output
    assert "rekol index rebuild" in result.output


def test_doctor_reports_corrupt_index_as_problem_not_crash(tmp_path: Path, monkeypatch) -> None:
    """A corrupt curated index must surface as a clean PROBLEM with a rebuild
    remedy — never an uncaught sqlite3.DatabaseError traceback. doctor is the tool
    a user runs WHEN memory looks broken, so it must diagnose corruption, not add
    to it.
    """
    home = _write_memory_home(tmp_path, monkeypatch)
    _build_curated_index(home)
    cfg = load_config()
    # Overwrite the real DB with bytes that are not a SQLite database at all.
    cfg.index_db_path.write_bytes(b"this is not a sqlite database\n" * 8)

    result = CliRunner().invoke(doctor_main, [])
    assert result.exit_code == 1, result.output
    # A crash would set result.exception to the DatabaseError; a clean diagnosis
    # exits via sys.exit(1) (SystemExit) with the finding printed.
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "corrupt" in result.output.lower()
    assert "rekol index rebuild" in result.output


def test_doctor_flags_session_fts_desync(tmp_path: Path, monkeypatch) -> None:
    """A healthy curated index but a desynced session FTS → exit 1 with the
    session remedy."""
    home = _write_memory_home(tmp_path, monkeypatch)
    _build_curated_index(home)
    runner = CliRunner()
    assert runner.invoke(session_index_main, ["--full"]).exit_code == 0

    # Corrupt the session FTS into the #18 orphaned state: drop triggers, wipe
    # messages, reinsert under fresh rowids the FTS index never saw.
    cfg = load_config()
    store = SessionStore(db_path=cfg.sessions_db_path, dim=384)
    store.init_schema()
    store.conn.executescript(
        "DROP TRIGGER IF EXISTS messages_ai;"
        "DROP TRIGGER IF EXISTS messages_ad;"
        "DROP TRIGGER IF EXISTS messages_au;"
    )
    store.conn.execute("DELETE FROM messages")
    store.conn.commit()
    store.conn.execute(
        "INSERT INTO messages(session_id, message_uuid, parent_uuid, role, content, "
        "cwd, timestamp_iso, timestamp_unix, jsonl_path, line_number) "
        "VALUES('s2','v1',NULL,'user','uninstall again','/tmp','2026-01-01T00:00:00Z',1,'/f',1)"
    )
    store.conn.commit()
    store.close()

    result = runner.invoke(doctor_main, [])
    assert result.exit_code == 1, result.output
    assert "session FTS" in result.output
    assert "rekol session-index --full" in result.output


def test_doctor_flags_partial_session_embeddings(tmp_path: Path, monkeypatch) -> None:
    """Messages ingested FTS-only (no embeddings) → embedding-coverage PROBLEM."""
    home = _write_memory_home(tmp_path, monkeypatch)
    _build_curated_index(home)
    runner = CliRunner()
    # --no-embed leaves the vector index empty while messages exist.
    assert runner.invoke(session_index_main, ["--full", "--no-embed"]).exit_code == 0

    result = runner.invoke(doctor_main, [])
    assert result.exit_code == 1, result.output
    assert "session embeddings" in result.output
    assert "rekol session-index --full" in result.output


def test_doctor_does_not_crash_on_missing_session_index(tmp_path: Path, monkeypatch) -> None:
    """A built curated index but no transcripts/sessions DB must not crash; the
    session check is reported as a finding (INFO), curated stays healthy."""
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    _build_curated_index(home)
    # Sessions DB never built (no transcripts ingested).
    result = CliRunner().invoke(doctor_main, [])
    assert result.exit_code == 0, result.output
    assert "session index" in result.output
    assert "healthy" in result.output


# ----------------------------- archive health ------------------------------


def test_doctor_reports_archive_present(tmp_path, monkeypatch):
    from rekol.cli_doctor import _check_archive
    from rekol.config import load_config

    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(tmp_path / "archive"))
    (tmp_path / "archive" / "projA").mkdir(parents=True)
    (tmp_path / "archive" / "projA" / "s.jsonl").write_text('{"type":"user"}\n')

    cfg = load_config()
    findings = _check_archive(cfg)
    archive_findings = [f for f in findings if "archive" in f.label]
    assert archive_findings, [f.label for f in findings]
    detail = " ".join(f.detail for f in archive_findings)
    assert "1" in detail  # one archived session counted


def test_doctor_warns_on_cloud_synced_archive(tmp_path, monkeypatch):
    """A synced archive puts verbatim secrets in the cloud and on-demand sync can
    dehydrate files — doctor must flag it (PROBLEM/warn), not stay silent."""
    from rekol.cli_doctor import _check_archive
    from rekol.config import load_config

    cloud = tmp_path / "Dropbox" / "rekol-archive"
    cloud.mkdir(parents=True)
    monkeypatch.setenv("REKOL_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(cloud))

    cfg = load_config()
    findings = _check_archive(cfg)
    detail = " ".join(f.detail.lower() for f in findings)
    assert "cloud" in detail or "sync" in detail


def test_doctor_warns_on_icloud_synced_archive(tmp_path, monkeypatch):
    """The iCloud candidate's REAL path is `…/Mobile Documents/com~apple~CloudDocs`,
    which does not contain the display label's first word ('iCloud'). Matching on
    the label word silently misses it — doctor must match the resolved archive path
    against the candidate PATH VALUES (or the iCloud on-disk signal)."""
    from rekol.cli_doctor import _check_archive
    from rekol.config import load_config

    # Mirror the macOS iCloud Drive on-disk layout under a fake HOME so the
    # candidate path resolves to this archive.
    fake_home = tmp_path / "home"
    icloud = fake_home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "rekol-archive"
    icloud.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("REKOL_HOME", str(tmp_path / "memhome"))
    (tmp_path / "memhome").mkdir()
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(icloud))

    cfg = load_config()
    findings = _check_archive(cfg)
    location_findings = [f for f in findings if f.label == "archive location"]
    assert location_findings, [f.label for f in findings]
    detail = " ".join(f.detail.lower() for f in location_findings)
    assert "cloud" in detail or "sync" in detail or "icloud" in detail


def test_doctor_reports_include_scope_coverage(tmp_path: Path, monkeypatch) -> None:
    """When include_dirs are configured, doctor reports an indexed-vs-discoverable
    coverage line (T8 #63) — the "Z of ~Y discoverable files indexed (~C%)" signal.
    """
    from rekol.include_indexer import index_include_dirs

    home = tmp_path / "home"
    home.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    research = tmp_path / "research"
    (research / "papers").mkdir(parents=True)
    (research / "papers" / "a.md").write_text("alpha\n", encoding="utf-8")
    (research / "papers" / "b.md").write_text("bravo\n", encoding="utf-8")
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\nclaude_projects_dir: {projects}\n"
        f"include_dirs:\n  - {research}\ninclude_deny:\n  []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REKOL_HOME", str(home))

    cfg = load_config()
    index_include_dirs(cfg)  # convert -> 100% coverage
    report = run_doctor(cfg, HashingEmbedder(dim=384))
    coverage = [f for f in report.findings if f.label == "include scope"]
    assert coverage, [f.label for f in report.findings]
    detail = coverage[0].detail.lower()
    assert "discoverable" in detail and "indexed" in detail
    assert "2 of" in detail


def test_doctor_silent_on_include_scope_when_unconfigured(tmp_path: Path, monkeypatch) -> None:
    """With no include_dirs (the default), doctor must NOT emit a coverage finding —
    additive feature, no noise for existing users (spec: silent-skip empty sources).
    """
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    _build_curated_index(home)
    cfg = load_config()
    report = run_doctor(cfg, HashingEmbedder(dim=384))
    assert not [f for f in report.findings if f.label == "include scope"]


# --------------------------- curated coverage (#123) ---------------------------


def test_doctor_curated_coverage_ok_when_all_indexed(tmp_path: Path, monkeypatch) -> None:
    """Every on-disk curated file indexed → coverage OK, counts match."""
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    _build_curated_index(home)
    cfg = load_config()
    report = run_doctor(cfg, HashingEmbedder(dim=384))
    coverage = [f for f in report.findings if f.label == "curated coverage"]
    assert len(coverage) == 1
    assert coverage[0].status is Status.OK
    assert "2/2" in coverage[0].detail  # both seeded files indexed


def test_doctor_flags_rejected_file_invisible_to_search(tmp_path: Path, monkeypatch) -> None:
    """A file the scanner rejects is on disk but not indexed — coverage must name it
    with its reason, mark the report degraded, and the CLI must exit 1 (#123).
    """
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    # Nested unknown type is not mapped (#123 part 3), so it never enters the index
    # and would otherwise be silently invisible to search.
    (home / "topics" / "broken.md").write_text(
        "---\nname: broken\ndescription: unindexable\nmetadata:\n  type: bogus\n---\n\nbody\n"
    )
    _build_curated_index(home)  # rebuild skips the broken file, does not crash
    cfg = load_config()
    report = run_doctor(cfg, HashingEmbedder(dim=384))
    coverage = [f for f in report.findings if f.label == "curated coverage"]
    assert len(coverage) == 1
    assert coverage[0].status is Status.PROBLEM
    assert "topics/broken.md" in coverage[0].detail
    assert "invisible to search" in coverage[0].detail
    assert "2/3" in coverage[0].detail  # 2 of 3 on-disk files indexed
    assert report.is_healthy is False

    result = CliRunner().invoke(doctor_main, [])
    assert result.exit_code == 1, result.output
    assert "topics/broken.md" in result.output
    assert "rekol index update" in result.output


# ------------- #157/#158: the denominator must come from the DISK -------------
# The coverage check used to derive its denominator from the indexer's own walk,
# so it compared the index against itself and structurally could not report a
# file the indexer never discovered — it printed "52/52 indexed (none rejected)"
# on a store where 10 files were unreachable by search.


def test_doctor_reports_a_file_the_indexer_never_walks(tmp_path: Path, monkeypatch) -> None:
    """#158: the check must catch a file OUTSIDE the indexer's scope.

    This is the structural guarantee, not a fix for one directory: a layer added
    to the store tomorrow that the walk does not know about must show up here.
    Uses a directory name the indexer has no knowledge of, so the test keeps
    working after `feedback`/flat-`projects` were added to the walk.
    """
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    (home / "notalayer").mkdir()
    (home / "notalayer" / "orphan.md").write_text(
        "---\nname: Orphan\ndescription: valid but unreachable\ntype: topic\n---\n\nbody\n"
    )
    _build_curated_index(home)

    report = run_doctor(load_config(), HashingEmbedder(dim=384))
    coverage = [f for f in report.findings if f.label == "curated coverage"]
    assert len(coverage) == 1
    assert coverage[0].status is Status.PROBLEM, coverage[0].detail
    assert "notalayer/orphan.md" in coverage[0].detail
    # Named as a SCOPE bug, so the remedy isn't "fix your frontmatter" — the file
    # is perfectly valid.
    assert "NEVER WALKED" in coverage[0].detail
    assert "2/3" in coverage[0].detail


def test_feedback_layer_is_walked_and_indexed(tmp_path: Path, monkeypatch) -> None:
    """#157: `feedback/` holds the behavioural-correction layer. It was never
    walked, so 0 of its facts were retrievable by search — reachable only via the
    MEMORY.md pointer a session might not follow."""
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    (home / "feedback").mkdir()
    (home / "feedback" / "correction.md").write_text(
        "---\nname: Correction\ndescription: how to work\ntype: feedback\n---\n\nbody\n"
    )
    _build_curated_index(home)

    report = run_doctor(load_config(), HashingEmbedder(dim=384))
    coverage = [f for f in report.findings if f.label == "curated coverage"]
    assert coverage[0].status is Status.OK, coverage[0].detail
    assert "3/3" in coverage[0].detail


def test_flat_project_files_are_walked(tmp_path: Path, monkeypatch) -> None:
    """A real store holds BOTH `projects/<name>.md` (migrated legacy "project"
    memories) and `projects/<slug>/<layer>/`. Only the nested form was walked."""
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    (home / "projects").mkdir()
    (home / "projects" / "flat.md").write_text(
        "---\nname: Flat\ndescription: a project memory\ntype: project\n---\n\nbody\n"
    )
    _build_curated_index(home)

    report = run_doctor(load_config(), HashingEmbedder(dim=384))
    coverage = [f for f in report.findings if f.label == "curated coverage"]
    assert coverage[0].status is Status.OK, coverage[0].detail
    assert "3/3" in coverage[0].detail


def test_tasks_and_memory_md_are_excluded_not_rejected(tmp_path: Path, monkeypatch) -> None:
    """`tasks/` is operational state and `MEMORY.md` carries no frontmatter by
    design. Both must be excluded EXPLICITLY and counted out loud — reporting them
    as rejections would make doctor permanently red for files working as intended,
    and saying nothing is what let "52/52" read as complete."""
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    (home / "tasks").mkdir()
    (home / "tasks" / "some-task.md").write_text("---\nid: t1\nstatus: open\n---\n\nwork\n")
    (home / "MEMORY.md").write_text("# Memory Index\n\n- pointer\n")
    _build_curated_index(home)

    report = run_doctor(load_config(), HashingEmbedder(dim=384))
    coverage = [f for f in report.findings if f.label == "curated coverage"]
    assert coverage[0].status is Status.OK, coverage[0].detail
    assert "2/2" in coverage[0].detail
    assert "2 deliberately excluded" in coverage[0].detail


def test_skip_manifest_counts_unwalked_files(tmp_path: Path, monkeypatch) -> None:
    """#158's other half: the SessionStart banner read the same wrong denominator,
    so it said "1 memory file invisible" when the real number was 10."""
    import json

    from rekol.config import SKIP_MANIFEST_NAME

    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    (home / "notalayer").mkdir()
    (home / "notalayer" / "orphan.md").write_text(
        "---\nname: Orphan\ndescription: unreachable\ntype: topic\n---\n\nbody\n"
    )
    (home / "tasks").mkdir()
    (home / "tasks" / "t.md").write_text("---\nid: t1\nstatus: open\n---\n\nwork\n")
    _build_curated_index(home)

    manifest = json.loads((load_config().index_dir / SKIP_MANIFEST_NAME).read_text())
    assert manifest["count"] == 1, manifest
    assert "notalayer/orphan.md" in manifest["paths"]
    # The deliberate exclusion must NOT inflate the banner.
    assert not any("tasks/" in p for p in manifest["paths"])


# ----------------------- #27: install drift in doctor -------------------------


def _seed_manifest(home: Path, **kv: str) -> None:
    d = home / ".install-logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.env").write_text(
        "\n".join(f"{k}={v}" for k, v in kv.items()) + "\n", encoding="utf-8"
    )


def _seed_settings(tmp_path: Path, monkeypatch, handlers: list[str]) -> None:
    """Point CLAUDE_CONFIG_DIR at a sandbox holding a settings.json we control."""
    import json as _json
    import sys

    cc = tmp_path / "claudeconfig"
    cc.mkdir(parents=True, exist_ok=True)
    (cc / "settings.json").write_text(
        _json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    # Render the real #159 form with a fallback that
                                    # genuinely runs, so this fixture isolates the
                                    # property under test (version drift) from hook
                                    # executability, which is graded separately.
                                    "command": f"\"$(command -v rekol || echo '{sys.executable}')\" "
                                    f"_hook {h} 2>/dev/null || true",
                                }
                            ]
                        }
                        for h in handlers
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))


def test_doctor_flags_unwired_handlers_as_a_problem(tmp_path: Path, monkeypatch) -> None:
    """The bug this feature exists for: handlers ship, nothing registers them."""
    from rekol import __version__
    from rekol.cli_doctor import _check_install_drift
    from rekol.update import expected_handlers

    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    _seed_manifest(home, VERSION=__version__, INSTALLED_AT="20260601-000000")
    _seed_settings(tmp_path, monkeypatch, sorted(expected_handlers())[1:])  # one short

    findings = _check_install_drift(load_config())
    wiring = [f for f in findings if f.label == "hook wiring"]
    assert len(wiring) == 1
    assert wiring[0].status is Status.PROBLEM, wiring[0].detail
    assert "install.sh" in (wiring[0].remedy or "")


def test_doctor_reports_version_drift_as_info_not_problem(tmp_path: Path, monkeypatch) -> None:
    """A dev checkout drifts from its recorded install on every `git pull`. Making
    that a PROBLEM would leave doctor permanently red for anyone working on rekol,
    and a check that is always red is a check nobody reads. The actionable failure
    is missing wiring, which is graded separately."""
    from rekol.cli_doctor import _check_install_drift
    from rekol.update import expected_handlers

    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    _seed_manifest(home, VERSION="0.3.1", INSTALLED_AT="20260601-000000")
    _seed_settings(tmp_path, monkeypatch, sorted(expected_handlers()))

    findings = _check_install_drift(load_config())
    version = [f for f in findings if f.label == "install version"]
    assert len(version) == 1
    assert version[0].status is Status.INFO
    assert "0.3.1" in version[0].detail
    assert all(f.status is not Status.PROBLEM for f in findings)


def test_doctor_says_drift_unknown_when_never_installed(tmp_path: Path, monkeypatch) -> None:
    """No manifest means install.sh never ran here. Reporting 'no drift' would be
    a claim about wiring we have no evidence for."""
    from rekol.cli_doctor import _check_install_drift

    _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    findings = _check_install_drift(load_config())
    assert len(findings) == 1
    assert findings[0].label == "install record"
    assert "drift unknown" in findings[0].detail
    assert findings[0].status is Status.INFO


def test_doctor_flags_valid_but_unindexed_files_as_a_problem(tmp_path: Path, monkeypatch) -> None:
    """#158's residual, found by review: fixing the DENOMINATOR was not enough.

    The verdict stayed keyed on `rejected` alone, so `1/6 curated files indexed
    (none rejected)` printed a green tick and "index is healthy" while five of six
    files were unreachable by search. Computing a number, printing it, and then not
    judging it is the same class of bug one level up.

    Valid-but-unindexed is only transient if a reindex actually runs — and in the
    #159 world the reindex hook was dead, so it was indefinite.
    """
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    _build_curated_index(home)
    # Add valid files AFTER indexing and do not reindex.
    for n in (1, 2, 3):
        (home / "knowledge").mkdir(exist_ok=True)
        (home / "knowledge" / f"svc{n}.md").write_text(
            f"---\nname: Svc{n}\ndescription: runbook\ntype: knowledge\n---\n\nbody {n}\n"
        )

    report = run_doctor(load_config(), HashingEmbedder(dim=384))
    coverage = [f for f in report.findings if f.label == "curated coverage"]
    assert len(coverage) == 1
    assert coverage[0].status is Status.PROBLEM, coverage[0].detail
    assert "2/5" in coverage[0].detail
    assert "invisible to search" in coverage[0].detail
    assert not report.is_healthy, "doctor must not claim health with unsearchable files"


# ----------------- #165: "index is healthy" with nothing indexed --------------


def test_claude_projects_dir_default_follows_claude_config_dir(tmp_path, monkeypatch) -> None:
    """Claude Code relocates its whole tree via CLAUDE_CONFIG_DIR. Hardcoding
    ~/.claude/projects meant a relocated install pointed at a directory that does
    not exist, so session-index exited 2 on every SessionEnd — into /dev/null,
    forever — while doctor still said healthy."""
    home = tmp_path / "home"
    (home / "topics").mkdir(parents=True)
    (home / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")
    monkeypatch.setenv("REKOL_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "relocated"))

    assert load_config().claude_projects_dir == tmp_path / "relocated" / "projects"


def test_explicit_claude_projects_dir_still_wins(tmp_path, monkeypatch) -> None:
    """An explicit config value means the user said where it is — do not override."""
    home = tmp_path / "home"
    (home / "topics").mkdir(parents=True)
    chosen = tmp_path / "somewhere-else"
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\nclaude_projects_dir: {chosen}\n"
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "relocated"))

    assert load_config().claude_projects_dir == chosen


def test_doctor_flags_a_missing_transcript_source_as_a_problem(tmp_path, monkeypatch) -> None:
    """A configured-but-absent projects dir makes session-index exit 2 forever, so
    the index can NEVER build. Reporting that as INFO is how doctor printed
    'index is healthy' and exited 0 for an install with zero transcripts indexed."""
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\nsession_search_enabled: true\n"
        f"claude_projects_dir: {tmp_path / 'does-not-exist'}\n"
    )
    _build_curated_index(home)

    report = run_doctor(load_config(), HashingEmbedder(dim=384))
    src = [f for f in report.findings if f.label == "transcript source"]
    assert len(src) == 1
    assert src[0].status is Status.PROBLEM, src[0].detail
    assert "does not exist" in src[0].detail
    assert not report.is_healthy


def test_doctor_flags_unindexed_transcripts_but_not_an_empty_source(tmp_path, monkeypatch) -> None:
    """'not built yet' is only benign when there is nothing to index. With
    transcripts sitting there unindexed, session search silently returns nothing."""
    home = _write_memory_home(tmp_path, monkeypatch, with_transcripts=False)
    projects = tmp_path / "projects"
    (projects / "proj").mkdir(parents=True, exist_ok=True)
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\nsession_search_enabled: true\n"
        f"claude_projects_dir: {projects}\n"
    )
    _build_curated_index(home)

    # Empty projects dir → genuinely nothing to index → INFO, still healthy.
    report = run_doctor(load_config(), HashingEmbedder(dim=384))
    idx = [f for f in report.findings if f.label == "session index"][0]
    assert idx.status is Status.INFO, idx.detail

    # Now a transcript exists and is unindexed → PROBLEM.
    (projects / "proj" / "session.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},'
        '"uuid":"u-1","timestamp":"2026-04-24T01:29:01.303Z","sessionId":"s-1","cwd":"/tmp/r"}\n'
    )
    report = run_doctor(load_config(), HashingEmbedder(dim=384))
    idx = [f for f in report.findings if f.label == "session index"][0]
    assert idx.status is Status.PROBLEM, idx.detail
    assert "unsearchable" in idx.detail
    assert not report.is_healthy


# --------------------------- fresh-install DEGRADED ---------------------------


def test_shipped_index_files_are_classified_non_memory() -> None:
    """Unit check on the classifier only — named for exactly what it executes.

    It was previously called ``..._are_excluded_from_coverage`` and its docstring
    claimed a pristine install is healthy and exits 0. It never ran `doctor` and
    never built an install, so it could not support that claim (#194, found in
    external review). The behavioural claim now lives in
    :func:`test_pristine_shipped_install_is_healthy_and_exits_zero`, and this one
    keeps the cheap constant-level assertion under an honest name.
    """
    from rekol.indexer import NON_MEMORY_FILES, is_non_memory

    root = Path("/tmp/does-not-need-to-exist")
    assert "REKOL.md" in NON_MEMORY_FILES
    assert is_non_memory(root / "REKOL.md", root)
    assert is_non_memory(root / "MEMORY.md", root)
    # ...and ordinary memory files are still counted.
    assert not is_non_memory(root / "when" / "when-foo.md", root)


def _write_pristine_shipped_home(tmp_path: Path, monkeypatch) -> Path:
    """A REKOL_HOME seeded the way `install.sh` seeds one, from the REAL template.

    install.sh does `cp -R src/rekol/template/* "$REKOL_HOME"/`, so copying that
    same directory is what makes this a pristine *install* rather than a
    hand-built fixture that happens to contain a file called REKOL.md. A fixture
    assembled by hand is exactly how the original defect survived its test: the
    shipped file was never present on the path under test.
    """
    import shutil

    import rekol

    home = tmp_path / "pristine"
    home.mkdir()
    template = Path(rekol.__file__).resolve().parent / "template"
    assert (template / "REKOL.md").is_file(), "the shipped template lost REKOL.md"
    shutil.copytree(template, home, dirs_exist_ok=True)

    # One real memory file: a brand-new store with nothing in it is a different
    # condition (empty index), and conflating the two would let this test pass for
    # the wrong reason.
    (home / "topics").mkdir(exist_ok=True)
    (home / "topics" / "prometheus.md").write_text(
        "---\nname: Prometheus\ndescription: metrics\ntype: topic\n"
        "tags: ['prometheus']\naliases: ['prom']\n---\n\n# Prometheus\n\nURL in IaC repo.\n"
    )
    # A real install has Claude Code's projects tree; without it `doctor` reports a
    # transcript-source PROBLEM and exits 1 for a reason that has nothing to do with
    # exclusions, which would make this test fail (or pass) on the wrong condition.
    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True, exist_ok=True)
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\nclaude_projects_dir: {projects.parent}\n"
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    return home


def test_pristine_shipped_install_is_healthy_and_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """The regression #188 actually promised: day-zero `doctor` is green.

    QA found a pristine install reporting `index is DEGRADED` and exiting 1,
    because `REKOL.md` — which rekol ships, seeds from its own installer, and
    keeps outside the indexer's scope on purpose — was missing from the exclusion
    list. Every new user's first `doctor` was red, with a remedy ("fix the
    frontmatter") that could not work. A checker that is red out of the box for a
    non-problem trains people to ignore it, burying the signal it exists to give.

    This drives the real Click command over a home seeded from the shipped
    template, so the shipped `REKOL.md` is genuinely on disk and genuinely
    considered — the thing the old constants-only test could not do.
    """
    home = _write_pristine_shipped_home(tmp_path, monkeypatch)
    _build_curated_index(home)

    result = CliRunner().invoke(doctor_main, [])

    assert result.exit_code == 0, result.output
    assert "index is healthy." in result.output
    assert "DEGRADED" not in result.output
    # And specifically: the shipped file is accounted for as excluded, never as a
    # curated file that search cannot reach.
    coverage = [line for line in result.output.splitlines() if "curated files indexed" in line]
    assert coverage, result.output
    assert "deliberately excluded" in coverage[0], coverage[0]


def test_doctor_excluded_line_renders_every_real_exclusion(tmp_path: Path, monkeypatch) -> None:
    """The RENDERED line must match the exclusion constants, not merely be an f-string.

    The previous version of this test read `cli_doctor`'s source for the literal
    ``deliberately excluded ({excluded_names})`` — so it would have passed just as
    happily if ``excluded_names`` were assigned a hardcoded, stale string. That is
    a check whose result is identical whether or not the promised property holds,
    which is the very bug class the line exists to prevent (#194).

    Asserting on `doctor`'s actual output instead means hardcoding a stale list
    fails here: the name that went missing (`REKOL.md`, when it was added) would
    be absent from the rendered text.
    """
    from rekol.indexer import NON_MEMORY_DIRS_DISPLAY, NON_MEMORY_FILES

    home = _write_pristine_shipped_home(tmp_path, monkeypatch)
    # `tasks/` must exist for the dirs half of the list to be exercised too.
    (home / "tasks").mkdir(exist_ok=True)
    (home / "tasks" / "a-task.md").write_text("---\nname: T\ndescription: d\n---\n\nbody\n")
    _build_curated_index(home)

    result = CliRunner().invoke(doctor_main, [])
    assert result.exit_code == 0, result.output

    coverage = [line for line in result.output.splitlines() if "deliberately excluded" in line]
    assert coverage, f"no excluded-files line rendered:\n{result.output}"
    rendered = coverage[0]
    for name in (*NON_MEMORY_DIRS_DISPLAY, *NON_MEMORY_FILES):
        assert name in rendered, (
            f"{name!r} is in the exclusion constants but missing from doctor's "
            f"rendered line {rendered!r} — the line has drifted from the real list"
        )


# ------------------------ two indexes in one store -------------------------
# `MEMORY.md` -> `REKOL.md` was a rename with no migration. The SessionStart hook
# prefers REKOL.md and falls back to MEMORY.md — so the fallback IS the whole
# migration strategy, and it only fires when REKOL.md is absent. On a store where
# both exist, MEMORY.md is never read and whatever lives only there is silently
# unreachable. Found on a live machine by shipwright, 2026-10-08: the two files had
# diverged and neither was a superset.
#
# Fourth instance of this same half-finished rename, after #177 (skill.md), #188
# (NON_MEMORY_FILES) and the doctor exclusion prose.


def _store(tmp_path, *, rekol_md: str | None, memory_md: str | None):
    """A curated store with either, both, or neither index file present."""
    for layer in ("always", "when", "topics", "knowledge"):
        (tmp_path / layer).mkdir(parents=True, exist_ok=True)
    if rekol_md is not None:
        (tmp_path / "REKOL.md").write_text(rekol_md)
    if memory_md is not None:
        (tmp_path / "MEMORY.md").write_text(memory_md)
    return tmp_path


def test_both_index_files_present_is_a_problem(tmp_path) -> None:
    """Only REKOL.md is read, so MEMORY.md's content is silently unreachable."""
    from rekol.cli_doctor import Status, _check_index_file_migration

    root = _store(tmp_path, rekol_md="# always-on rules\n", memory_md="# infra pointers\n")
    findings = _check_index_file_migration(root)

    assert len(findings) == 1
    f = findings[0]
    assert f.status is Status.PROBLEM
    # Must name BOTH files and say which one is actually read — a finding that
    # says only "two indexes" leaves the user guessing which to edit.
    assert "REKOL.md" in f.detail and "MEMORY.md" in f.detail
    assert f.remedy is not None


def test_only_rekol_md_is_healthy(tmp_path) -> None:
    """The migrated steady state reports nothing."""
    from rekol.cli_doctor import _check_index_file_migration

    root = _store(tmp_path, rekol_md="# rules\n", memory_md=None)
    assert _check_index_file_migration(root) == []


def test_only_memory_md_is_healthy(tmp_path) -> None:
    """An un-migrated store is fine: the hook's fallback reads MEMORY.md correctly.

    Flagging this would cry wolf on every store that simply has not been renamed
    yet, which is the healthy pre-migration state, not a fault.
    """
    from rekol.cli_doctor import _check_index_file_migration

    root = _store(tmp_path, rekol_md=None, memory_md="# rules\n")
    assert _check_index_file_migration(root) == []


def test_neither_index_file_is_not_this_check_s_business(tmp_path) -> None:
    """A store with no index at all is reported elsewhere, not duplicated here."""
    from rekol.cli_doctor import _check_index_file_migration

    root = _store(tmp_path, rekol_md=None, memory_md=None)
    assert _check_index_file_migration(root) == []


def test_identical_content_still_flagged_but_said_to_be_identical(tmp_path) -> None:
    """Byte-identical copies are safe to delete, and the remedy should say so.

    Still a finding — two files where one is ignored is a state the user should
    resolve — but the advice differs sharply from the diverged case, and telling
    someone to "merge" identical files wastes their time.
    """
    from rekol.cli_doctor import Status, _check_index_file_migration

    same = "# identical\n"
    root = _store(tmp_path, rekol_md=same, memory_md=same)
    findings = _check_index_file_migration(root)

    assert len(findings) == 1
    assert findings[0].status is Status.PROBLEM
    assert "identical" in (findings[0].detail + (findings[0].remedy or "")).lower()


# ---------------- orphaned console scripts on PATH (#203) ----------------
# A `rekol` console script can be left pointing at an interpreter where the
# package no longer resolves. An interactive shell resolves the WORKING wrapper
# first, so `command -v rekol` is green and doctor passed — while a hook, whose
# PATH order differs, resolves the orphan and the session starts with no memory.
#
# Classification is STATIC: nothing here executes a candidate. `--version` is not
# a safety boundary for an arbitrary program named `rekol`, and bounding each one
# still lets a scripted doctor stall. Fixtures build real interpreter trees so the
# inspection under test is the shipped inspection.
#
# Verdict table these pin (only ORPHAN may advise removal):
#   Python shebang, interpreter missing        -> orphan,       remedy
#   Python shebang, site-packages has no rekol -> orphan,       remedy
#   editable install, target missing rekol     -> orphan,       remedy
#   non-Python or env-resolved shebang         -> unclassified, NO remedy
#   site-packages not locatable                -> can't tell,   NO remedy
#   rekol resolves                             -> OK


def _interp_tree(root: Path, *, version: str = "3.11") -> Path:
    """A plausible venv layout: <root>/bin/pythonX.Y + <root>/lib/pythonX.Y/site-packages."""
    interp = root / "bin" / f"python{version}"
    interp.parent.mkdir(parents=True, exist_ok=True)
    interp.write_text("#!/bin/sh\nexit 0\n")
    interp.chmod(0o755)
    (root / "lib" / f"python{version}" / "site-packages").mkdir(parents=True, exist_ok=True)
    return interp


def _site_packages(root: Path, *, version: str = "3.11") -> Path:
    return root / "lib" / f"python{version}" / "site-packages"


def _script_for(directory: Path, interpreter: Path | str) -> Path:
    """A console script whose shebang names `interpreter` — never executed by the check."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "rekol"
    script.write_text(f"#!{interpreter}\nfrom rekol.cli import main\nmain()\n")
    script.chmod(0o755)
    return script


def test_orphan_when_the_shebang_interpreter_is_gone(tmp_path, monkeypatch) -> None:
    """A stale script whose interpreter was deleted. Exact evidence, so removal is advised.

    This form fails at `exec` with ENOENT and prints no Python traceback at all, so
    an output-matching classifier downgrades it to INFO and misses a real orphan.
    """
    from rekol.cli_doctor import _check_console_scripts

    script = _script_for(tmp_path / "bin", tmp_path / "deleted" / "bin" / "python3.11")
    monkeypatch.setenv("PATH", str(script.parent))

    findings = _check_console_scripts()
    assert findings[0].status is Status.PROBLEM, findings[0].detail
    assert "no longer exists" in findings[0].detail
    assert findings[0].remedy is not None


def test_orphan_when_site_packages_has_no_rekol(tmp_path, monkeypatch) -> None:
    from rekol.cli_doctor import _check_console_scripts

    interp = _interp_tree(tmp_path / "env")
    script = _script_for(tmp_path / "bin", interp)
    monkeypatch.setenv("PATH", str(script.parent))

    findings = _check_console_scripts()
    assert findings[0].status is Status.PROBLEM, findings[0].detail
    assert "not importable" in findings[0].detail
    assert findings[0].remedy is not None


def test_orphan_when_an_editable_install_points_at_a_deleted_tree(tmp_path, monkeypatch) -> None:
    """THE real #203 orphan: an editable install whose .pth target was deleted.

    Its `dist-info` is still in site-packages, so a check that looks only for
    `rekol-*.dist-info` calls it healthy — which is what made this the one form the
    first design missed. The .pth has to be followed to its target.
    """
    from rekol.cli_doctor import _check_console_scripts

    interp = _interp_tree(tmp_path / "env")
    sp = _site_packages(tmp_path / "env")
    (sp / "rekol-0.1.0.dist-info").mkdir()
    (sp / "rekol-0.1.0.dist-info" / "direct_url.json").write_text(
        '{"dir_info": {"editable": true}, "url": "file:///gone/worktree"}'
    )
    # Points at a tree that no longer exists — the deleted agent worktree.
    (sp / "__editable__.rekol-0.1.0.pth").write_text(str(tmp_path / "gone" / "src") + "\n")
    script = _script_for(tmp_path / "bin", interp)
    monkeypatch.setenv("PATH", str(script.parent))

    findings = _check_console_scripts()
    assert findings[0].status is Status.PROBLEM, findings[0].detail
    assert "no rekol/__init__.py" in findings[0].detail
    assert findings[0].remedy is not None


def test_an_editable_install_that_resolves_is_not_an_orphan(tmp_path, monkeypatch) -> None:
    """The same shape as Leon's live install, which must stay green."""
    from rekol.cli_doctor import _check_console_scripts

    interp = _interp_tree(tmp_path / "env")
    sp = _site_packages(tmp_path / "env")
    target = tmp_path / "checkout" / "src"
    (target / "rekol").mkdir(parents=True)
    (target / "rekol" / "__init__.py").write_text("")
    (sp / "__editable__.rekol-0.6.1.pth").write_text(str(target) + "\n")
    script = _script_for(tmp_path / "bin", interp)
    monkeypatch.setenv("PATH", str(script.parent))

    findings = _check_console_scripts()
    assert findings[0].status is Status.OK, findings[0].detail
    assert "resolve" in findings[0].detail


def test_the_bash_shim_is_never_classified_or_advised_for_removal(tmp_path, monkeypatch) -> None:
    """The shipped `bin/rekol` shim is `#!/usr/bin/env bash`.

    bash has no site-packages, so "absent means orphan" would fire on the user's
    own working shim and tell them to delete it. Two earlier versions of this
    check did exactly that in different ways, which is why it has its own test.
    """
    from rekol.cli_doctor import _check_console_scripts

    script = _script_for(tmp_path / "bin", "/usr/bin/env bash")
    monkeypatch.setenv("PATH", str(script.parent))

    findings = _check_console_scripts()
    assert findings[0].status is Status.INFO, findings[0].detail
    assert findings[0].remedy is None, "must never advise deleting the shipped shim"
    assert "rm " not in findings[0].detail


def test_env_resolved_python_is_not_classified(tmp_path, monkeypatch) -> None:
    """`#!/usr/bin/env python3` picks its interpreter from PATH at exec time.

    Which interpreter runs is therefore not knowable statically, so no verdict —
    and certainly no removal advice — can be justified.
    """
    from rekol.cli_doctor import _check_console_scripts

    script = _script_for(tmp_path / "bin", "/usr/bin/env python3")
    monkeypatch.setenv("PATH", str(script.parent))

    findings = _check_console_scripts()
    assert findings[0].status is Status.INFO, findings[0].detail
    assert findings[0].remedy is None


def test_unlocatable_site_packages_is_cant_tell_not_absent(tmp_path, monkeypatch) -> None:
    """ "Couldn't find it" must never mean "absent" — that is QA's F3 in another form.

    Locating site-packages without running the interpreter is layout guesswork
    (venv, framework, pyenv, Debian dist-packages, user site). When the guess
    fails the honest answer is "cannot tell", with no removal advice.
    """
    from rekol.cli_doctor import _check_console_scripts

    # An interpreter with no lib/ tree at all: nothing to read, so nothing to conclude.
    interp = tmp_path / "weird" / "bin" / "python3.11"
    interp.parent.mkdir(parents=True)
    interp.write_text("#!/bin/sh\nexit 0\n")
    interp.chmod(0o755)
    script = _script_for(tmp_path / "bin", interp)
    monkeypatch.setenv("PATH", str(script.parent))

    findings = _check_console_scripts()
    assert findings[0].status is Status.INFO, findings[0].detail
    assert findings[0].remedy is None
    assert "no site-packages" in findings[0].detail


def test_a_working_rekol_first_does_not_hide_an_orphan_after_it(tmp_path, monkeypatch) -> None:
    """THE #203 property: the orphan is reported even though the shell never reaches it.

    `shutil.which` — and a user's shell — stop at the first hit, so an orphan later
    on PATH is invisible while being exactly what a hook with different PATH order
    may resolve. Fails against a first-hit-only implementation.
    """
    from rekol.cli_doctor import _check_console_scripts

    good_env = _interp_tree(tmp_path / "good-env")
    (_site_packages(tmp_path / "good-env") / "rekol").mkdir()
    (_site_packages(tmp_path / "good-env") / "rekol" / "__init__.py").write_text("")
    good = _script_for(tmp_path / "good", good_env)

    orphan = _script_for(tmp_path / "orphan", tmp_path / "deleted" / "bin" / "python3.11")

    monkeypatch.setenv("PATH", os.pathsep.join([str(good.parent), str(orphan.parent)]))

    findings = _check_console_scripts()
    assert findings[0].status is Status.PROBLEM, findings[0].detail
    assert str(orphan) in findings[0].detail
    assert "1 of 2" in findings[0].detail


def test_all_resolving_rekol_on_path_is_ok(tmp_path, monkeypatch) -> None:
    from rekol.cli_doctor import _check_console_scripts

    interp = _interp_tree(tmp_path / "env")
    (_site_packages(tmp_path / "env") / "rekol").mkdir()
    (_site_packages(tmp_path / "env") / "rekol" / "__init__.py").write_text("")
    script = _script_for(tmp_path / "bin", interp)
    monkeypatch.setenv("PATH", str(script.parent))

    findings = _check_console_scripts()
    assert findings[0].status is Status.OK, findings[0].detail


def test_no_rekol_on_path_is_info_not_degraded(tmp_path, monkeypatch) -> None:
    """Must NOT be red: hooks fall back to the absolute path the installer recorded."""
    from rekol.cli_doctor import _check_console_scripts

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    findings = _check_console_scripts()
    assert findings[0].status is Status.INFO, findings[0].detail


def test_the_report_states_that_only_this_process_path_was_inspected(tmp_path, monkeypatch) -> None:
    """A green line must not read as "the hook path is proven".

    doctor sees only its own PATH; a hook runs with a different one. Saying so in
    the output is the difference between a true statement and an overclaim.
    """
    from rekol.cli_doctor import _check_console_scripts

    interp = _interp_tree(tmp_path / "env")
    (_site_packages(tmp_path / "env") / "rekol").mkdir()
    (_site_packages(tmp_path / "env") / "rekol" / "__init__.py").write_text("")
    script = _script_for(tmp_path / "bin", interp)
    monkeypatch.setenv("PATH", str(script.parent))

    detail = _check_console_scripts()[0].detail
    assert "only this process's PATH" in detail.lower() or "this process's PATH" in detail


def test_a_directory_named_rekol_is_not_mistaken_for_a_script(tmp_path, monkeypatch) -> None:
    """An executable DIRECTORY named `rekol` on PATH must not be probed as a program."""
    from rekol.cli_doctor import _rekol_scripts_on_path

    d = tmp_path / "bin" / "rekol"
    d.mkdir(parents=True)
    monkeypatch.setenv("PATH", str(d.parent))

    assert _rekol_scripts_on_path() == []


def test_the_same_script_reached_twice_is_reported_once(tmp_path, monkeypatch) -> None:
    """A symlink and its target are one program; listing it twice reads as two faults."""
    from rekol.cli_doctor import _rekol_scripts_on_path

    interp = _interp_tree(tmp_path / "env")
    real = _script_for(tmp_path / "real", interp)
    linkdir = tmp_path / "link"
    linkdir.mkdir()
    (linkdir / "rekol").symlink_to(real)
    monkeypatch.setenv("PATH", os.pathsep.join([str(linkdir), str(real.parent)]))

    assert len(_rekol_scripts_on_path()) == 1


# ---------------- `rekol --version` vs the code being run (#203) ----------------


def test_version_drift_is_reported_with_both_numbers(monkeypatch) -> None:
    """`--version` reads install-time metadata, so an editable install drifts.

    Measured on a live machine: `rekol --version` said 0.5.5 while the code being
    executed was 0.6.1, and that stale number was repeated in coordination notes
    for days.
    """
    import importlib.metadata as md

    from rekol import __version__ as code_version
    from rekol.cli_doctor import _check_version_metadata

    monkeypatch.setattr(md, "version", lambda _name: "0.0.1-stale")

    findings = _check_version_metadata()
    assert len(findings) == 1
    assert findings[0].status is Status.INFO
    assert "0.0.1-stale" in findings[0].detail
    assert code_version in findings[0].detail


def test_version_agreement_is_ok(monkeypatch) -> None:
    import importlib.metadata as md

    from rekol import __version__ as code_version
    from rekol.cli_doctor import _check_version_metadata

    monkeypatch.setattr(md, "version", lambda _name: code_version)

    findings = _check_version_metadata()
    assert findings[0].status is Status.OK


def test_missing_distribution_metadata_is_info_not_a_crash(monkeypatch) -> None:
    import importlib.metadata as md

    from rekol.cli_doctor import _check_version_metadata

    def _raise(_name):
        raise md.PackageNotFoundError("rekol")

    monkeypatch.setattr(md, "version", _raise)

    findings = _check_version_metadata()
    assert findings[0].status is Status.INFO
    assert "metadata" in findings[0].detail


# -------- the recorded hook path is independent evidence (#215) --------
# NOT a fix for #203's filed split: see the test docstring. In that state
# `command -v` succeeds on the orphan, so the recorded fallback is never
# consulted and nothing here fires. Preferring the recorded path is what
# closes it, in the hook-order PR.


def test_doctor_reports_a_wired_hook_whose_recorded_path_is_broken(tmp_path, monkeypatch) -> None:
    """A wired hook whose recorded fallback cannot run, with doctor's PATH healthy.

    What this proves: doctor does not report healthy just because its OWN PATH is
    fine. The recorded path is independent evidence, and it is checked even with
    no install manifest present (#215).

    What this does NOT prove, and must not be read as: closing #203. In the filed
    failure the orphan is FIRST on the hook's PATH, so `command -v rekol`
    *succeeds*, the orphan runs, and the recorded fallback — which is perfectly
    healthy — is never reached. `unrunnable_hooks()` therefore passes, and the
    orphan is absent from doctor's PATH, so the PATH walk passes too. Neither
    check fires.

    Only preferring the recorded path over the PATH lookup prevents that, which is
    the hook-order change. The regression for the filed split belongs there, and
    #214 is detection only.
    """
    from rekol.cli_doctor import _check_console_scripts, _check_hook_executability

    # 1. doctor's PATH: one healthy, resolvable rekol. Nothing wrong here.
    env_root = tmp_path / "good-env"
    interp = _interp_tree(env_root)
    (_site_packages(env_root) / "rekol").mkdir()
    (_site_packages(env_root) / "rekol" / "__init__.py").write_text("")
    good = _script_for(tmp_path / "shellbin", interp)
    monkeypatch.setenv("PATH", str(good.parent))

    # 2. The hook's recorded invocation points somewhere that does not exist.
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    missing = tmp_path / "gone" / "bin" / "rekol"
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        f"\"$(command -v rekol || echo '{missing}')\" "
                                        "_hook session-start 2>/dev/null || true"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    # The PATH walk alone is happy — that is precisely the blind spot.
    assert _check_console_scripts()[0].status is Status.OK

    # The hook check is what must refuse to call this healthy.
    hook_findings = _check_hook_executability()
    assert hook_findings[0].status is Status.PROBLEM, hook_findings[0].detail
    assert "cannot run" in hook_findings[0].detail
    # And it must not overclaim: this verifies the recorded FALLBACK, not which
    # executable a hook resolves. The wording is load-bearing, so pin it.
    assert "fallback" in hook_findings[0].detail
    assert "not checked here" in hook_findings[0].detail


def test_a_relative_path_editable_install_is_never_an_orphan(tmp_path, monkeypatch) -> None:
    """A `.pth` naming a RELATIVE path must not produce removal advice.

    Resolution is attempted against site-packages as a best effort, but the real
    base is whichever `sys.path` entry the interpreter used, so a miss is
    cannot-tell — not proof the package is gone. Codex reproduced ORPHAN (with
    removal advice) for this layout against the previous implementation.
    """
    from rekol.cli_doctor import _check_console_scripts

    env_root = tmp_path / "env"
    interp = _interp_tree(env_root)
    sp = _site_packages(env_root)
    # Resolves relative to site-packages: ../../../checkout/src
    target = env_root / "checkout" / "src"
    (target / "rekol").mkdir(parents=True)
    (target / "rekol" / "__init__.py").write_text("")
    (sp / "__editable__.rekol-0.6.1.pth").write_text("../../../checkout/src\n")
    script = _script_for(tmp_path / "bin", interp)
    monkeypatch.setenv("PATH", str(script.parent))

    finding = _check_console_scripts()[0]
    assert finding.status is not Status.PROBLEM, finding.detail
    assert finding.remedy is None


def test_a_setuptools_finder_pth_is_cant_tell_not_an_orphan(tmp_path, monkeypatch) -> None:
    """A finder `.pth` maps modules through an import hook, so it is not parsed.

    `import __editable___rekol_finder` resolves rekol at runtime through code we
    would have to execute or reimplement. Codex reproduced ORPHAN plus removal
    advice for this layout, which is a completely healthy editable install.
    """
    from rekol.cli_doctor import _check_console_scripts

    env_root = tmp_path / "env"
    interp = _interp_tree(env_root)
    sp = _site_packages(env_root)
    (sp / "rekol-0.6.1.dist-info").mkdir()
    (sp / "__editable__.rekol-0.6.1.pth").write_text(
        "import __editable___rekol_0_6_1_finder; __editable___rekol_0_6_1_finder.install()\n"
    )
    script = _script_for(tmp_path / "bin", interp)
    monkeypatch.setenv("PATH", str(script.parent))

    finding = _check_console_scripts()[0]
    assert finding.status is Status.INFO, finding.detail
    assert finding.remedy is None, "a finder .pth must never yield removal advice"
    assert "import hook" in finding.detail
