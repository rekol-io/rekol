"""CLI + end-to-end round-trip: convert a fixture tree, then ingest it for real."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from memory_tools.cli_docs_convert import main as cli_main

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
    from memory_tools.sessions.ingest import ingest_directory
    from memory_tools.sessions.store import SessionStore

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


def test_cli_missing_shim_exits_3(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "memhome"
    projects = tmp_path / "projects"
    _write_config(home, projects)
    monkeypatch.setenv("MEMORY_HOME", str(home))
    # Force the shim to appear absent so the --index path hits the exit-3 guard.
    monkeypatch.setattr("memory_tools.cli_docs_convert.shutil.which", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(cli_main, [str(FIXTURE_TREE), "--prefix", "arc", "--index"])
    assert result.exit_code == 3, result.output
    # JSONL was still written before the indexing attempt
    assert (projects / "arc" / "Cassandra-Ops.jsonl").exists()


def test_cli_index_chains_incremental(tmp_path: Path, monkeypatch) -> None:

    home = tmp_path / "memhome"
    projects = tmp_path / "projects"
    _write_config(home, projects)
    monkeypatch.setenv("MEMORY_HOME", str(home))
    monkeypatch.setattr(
        "memory_tools.cli_docs_convert.shutil.which", lambda _: "/fake/claude-session-index"
    )
    calls = {}

    def _fake_run(cmd, check=False):
        calls["cmd"] = cmd

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr("memory_tools.cli_docs_convert.subprocess.run", _fake_run)

    runner = CliRunner()
    result = runner.invoke(cli_main, [str(FIXTURE_TREE), "--prefix", "arc", "--index"])
    assert result.exit_code == 0, result.output
    assert calls["cmd"] == ["claude-session-index", "--incremental"]  # incremental, not full
