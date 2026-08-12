"""Tests for `rekol doctor` (C5 observability).

Sandbox-only: every test uses a throwaway ``REKOL_HOME`` and the autouse
``_isolate_index_cache`` fixture (conftest) points the cache at a temp dir, so
the real ``~/.cache`` / index is never touched.

The bar (from the C5 spec):
  * a good index reports healthy and exits 0;
  * a corrupt/empty/missing index exits 1 with an actionable remedy.
"""

from __future__ import annotations

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
