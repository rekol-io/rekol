"""CLI + end-to-end round-trip: convert a fixture tree, then ingest it for real."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from rekol.cli_docs_convert import main as cli_main

FIXTURE_TREE = Path(__file__).parent / "fixtures" / "docs_tree"


def _write_config(home: Path, projects: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "memory.config.yaml").write_text(
        f"claude_projects_dir: {projects}\nembedding_model: test-hashing\n"
    )


def test_cli_converts_tree_and_reports_stats(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "memhome"
    projects = tmp_path / "projects"
    _write_config(home, projects)
    monkeypatch.setenv("MEMORY_HOME", str(home))

    runner = CliRunner()
    result = runner.invoke(cli_main, [str(FIXTURE_TREE), "--prefix", "arc", "--no-index"])
    assert result.exit_code == 0, result.output
    assert "jsonl_written=1" in result.output
    assert "files_converted=2" in result.output
    assert (projects / "arc" / "Cassandra-Ops.jsonl").exists()


def test_cli_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "memhome"
    projects = tmp_path / "projects"
    _write_config(home, projects)
    monkeypatch.setenv("MEMORY_HOME", str(home))

    runner = CliRunner()
    result = runner.invoke(cli_main, [str(FIXTURE_TREE), "--prefix", "arc", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "jsonl_written=1" in result.output
    assert not (projects / "arc").exists()


def test_cli_missing_source_dir_exits_2(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "memhome"
    projects = tmp_path / "projects"
    _write_config(home, projects)
    monkeypatch.setenv("MEMORY_HOME", str(home))

    runner = CliRunner()
    result = runner.invoke(cli_main, [str(tmp_path / "nope"), "--no-index"])
    assert result.exit_code == 2


def test_roundtrip_converted_docs_are_searchable(tmp_path: Path, monkeypatch) -> None:
    """The load-bearing test: synthetic JSONL must satisfy the REAL ingester."""
    from rekol.sessions.ingest import ingest_directory
    from rekol.sessions.store import SessionStore

    home = tmp_path / "memhome"
    projects = tmp_path / "projects"
    _write_config(home, projects)
    monkeypatch.setenv("MEMORY_HOME", str(home))

    # Convert the fixture into projects/arc/*.jsonl
    runner = CliRunner()
    result = runner.invoke(cli_main, [str(FIXTURE_TREE), "--prefix", "arc", "--no-index"])
    assert result.exit_code == 0, result.output

    # Now run the real ingester over the projects dir and search
    db = home / ".index" / "sessions.db"
    with SessionStore(db_path=db, dim=384) as store:
        store.init_schema()
        stats = ingest_directory(projects, store, force=True)
        assert stats.messages_inserted == 2, vars(stats)
        hits = store.search_fts("cassandra cluster", top_k=5)
        assert any("health-2026-04-28.md" in h["content"] for h in hits)
        # role tag survives end-to-end
        assert hits, "expected at least one FTS hit"
        assert all(h["role"] == "document" for h in hits), [h["role"] for h in hits]


def test_cli_index_chains_incremental_in_process(tmp_path: Path, monkeypatch) -> None:
    """--index invokes session-index in-process (no PATH/subprocess dependency)."""
    home = tmp_path / "memhome"
    projects = tmp_path / "projects"
    _write_config(home, projects)
    monkeypatch.setenv("MEMORY_HOME", str(home))

    calls = {}

    def _fake_cli(args, standalone_mode=True):
        calls["args"] = args
        calls["standalone_mode"] = standalone_mode

    monkeypatch.setattr("rekol.cli.main", _fake_cli)

    runner = CliRunner()
    result = runner.invoke(cli_main, [str(FIXTURE_TREE), "--prefix", "arc", "--index"])
    assert result.exit_code == 0, result.output
    assert calls["args"] == ["session-index", "--incremental"]  # incremental, not full
    assert calls["standalone_mode"] is False  # Click returns/raises instead of sys.exit
    # JSONL was still written before the indexing call.
    assert (projects / "arc" / "Cassandra-Ops.jsonl").exists()


def test_cli_index_propagates_session_index_failure(tmp_path: Path, monkeypatch) -> None:
    """A non-zero SystemExit from the in-process session-index surfaces as exit code."""
    home = tmp_path / "memhome"
    projects = tmp_path / "projects"
    _write_config(home, projects)
    monkeypatch.setenv("MEMORY_HOME", str(home))

    def _fake_cli(args, standalone_mode=True):
        raise SystemExit(3)

    monkeypatch.setattr("rekol.cli.main", _fake_cli)

    runner = CliRunner()
    result = runner.invoke(cli_main, [str(FIXTURE_TREE), "--prefix", "arc", "--index"])
    assert result.exit_code == 3, result.output
    # JSONL was still written before the indexing attempt.
    assert (projects / "arc" / "Cassandra-Ops.jsonl").exists()
