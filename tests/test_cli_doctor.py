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
