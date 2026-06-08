"""End-to-end: session-index also indexes include_dirs, governed by the deny-list.

The acceptance criterion of #63: include a dir once -> its files (and any new
ones) auto-index on each session-index run; the deny-list governs; the content
is searchable. These drive the CLI, not the indexer in isolation.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cache_helpers import cache_dir_for
from rekol.cli_session_index import main as cli_main
from rekol.sessions.store import SessionStore


def _home_with_include(tmp_path: Path, monkeypatch, include_dirs, include_deny=()) -> Path:
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "fake-projects"
    projects.mkdir(parents=True)
    newline = "\n"
    dirs_yaml = "".join(f"  - {d}{newline}" for d in include_dirs) or f"  []{newline}"
    deny_yaml = "".join(f"  - {d}{newline}" for d in include_deny) or f"  []{newline}"
    (home / "rekol.config.yaml").write_text(
        f"claude_projects_dir: {projects}\n"
        "embedding_model: test-hashing\n"
        "archive_enabled: false\n"  # keep the e2e path simple: ingest from live
        f"include_dirs:\n{dirs_yaml}"
        f"include_deny:\n{deny_yaml}",
        encoding="utf-8",
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    return home


def _search(home: Path, term: str) -> int:
    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
    store.init_schema()
    try:
        return len(store.search_fts(term, top_k=10))
    finally:
        store.close()


def test_included_dir_content_is_searchable_after_index(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    (src / "papers").mkdir(parents=True)
    (src / "papers" / "hyp.md").write_text(
        "the tabularasa hypothesis statement\n", encoding="utf-8"
    )
    home = _home_with_include(tmp_path, monkeypatch, [src])

    result = CliRunner().invoke(cli_main, ["--full"])
    assert result.exit_code == 0, result.output
    # The CLI surfaces the include-scope stats line.
    assert "include_dirs_converted=1" in result.output
    # The included content is now keyword-searchable in the sessions index.
    assert _search(home, "tabularasa") == 1


def test_deny_keeps_denied_content_unsearchable(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    (src / "keep").mkdir(parents=True)
    (src / "keep" / "a.md").write_text("keepterm alpha\n", encoding="utf-8")
    (src / "old-projects").mkdir(parents=True)
    (src / "old-projects" / "b.md").write_text("denyterm bravo\n", encoding="utf-8")
    home = _home_with_include(tmp_path, monkeypatch, [src], include_deny=["old-projects"])

    result = CliRunner().invoke(cli_main, ["--full"])
    assert result.exit_code == 0, result.output
    assert _search(home, "keepterm") == 1
    assert _search(home, "denyterm") == 0


def test_new_file_in_included_dir_auto_indexes_next_run(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    (src / "papers").mkdir(parents=True)
    (src / "papers" / "a.md").write_text("alpha original\n", encoding="utf-8")
    home = _home_with_include(tmp_path, monkeypatch, [src])

    assert CliRunner().invoke(cli_main, ["--full"]).exit_code == 0
    assert _search(home, "freshterm") == 0

    # A dir included once -> a new file auto-indexes on the NEXT incremental run.
    (src / "papers" / "b.md").write_text("freshterm bravo\n", encoding="utf-8")
    res = CliRunner().invoke(cli_main, ["--incremental"])
    assert res.exit_code == 0, res.output
    assert "include_dirs_converted=1" in res.output
    assert _search(home, "freshterm") == 1


def test_unchanged_include_dir_skipped_on_rerun(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    (src / "papers").mkdir(parents=True)
    (src / "papers" / "a.md").write_text("stable content\n", encoding="utf-8")
    _home_with_include(tmp_path, monkeypatch, [src])

    assert CliRunner().invoke(cli_main, ["--full"]).exit_code == 0
    # Second run, no changes: the include dir is skipped (incremental).
    res = CliRunner().invoke(cli_main, ["--incremental"])
    assert res.exit_code == 0, res.output
    assert "include_dirs_skipped_unchanged=1" in res.output
    assert "include_dirs_converted=0" in res.output
