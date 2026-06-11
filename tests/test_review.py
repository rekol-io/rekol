"""Tests for durable-memory overdue detection and the review command."""

import datetime as dt
from pathlib import Path

from click.testing import CliRunner

from cache_helpers import cache_dir_for
from rekol.review import find_overdue

HOME = Path("/m")
TODAY = dt.date(2026, 6, 1)


def _rows(*specs):
    return [dict(file_path=f"/m/{lyr}/{n}.md", updated=u, created=None) for lyr, n, u in specs]


def test_overdue_durable_only_past_interval():
    rows = _rows(
        ("knowledge", "old", "2025-01-01"),  # >180d -> overdue
        ("knowledge", "fresh", "2026-05-20"),  # <180d -> ok
        ("topics", "old", "2020-01-01"),  # not durable -> ignored
    )
    out = find_overdue(
        rows,
        memory_home=HOME,
        exempt_layers=["always", "knowledge"],
        interval_days=180,
        today=TODAY,
    )
    assert [o["file_path"] for o in out] == ["/m/knowledge/old.md"]


def test_missing_date_is_overdue_and_sorts_first():
    rows = [
        dict(file_path="/m/always/x.md", updated=None, created=None),
        dict(file_path="/m/knowledge/y.md", updated="2024-01-01", created=None),
    ]
    out = find_overdue(
        rows,
        memory_home=HOME,
        exempt_layers=["always", "knowledge"],
        interval_days=180,
        today=TODAY,
    )
    assert out[0]["file_path"] == "/m/always/x.md" and out[0]["age_days"] is None


def _seed_index(home, layer, name, updated):
    import numpy as np

    from rekol.store import IndexStore

    (home / layer).mkdir(parents=True, exist_ok=True)
    fp_path = home / layer / f"{name}.md"
    fp_path.write_text(
        f"---\nname: {name}\ndescription: d\ntype: knowledge\nupdated: {updated}\n---\nbody\n"
    )
    idx = cache_dir_for(home)
    idx.mkdir(parents=True, exist_ok=True)
    store = IndexStore(db_path=idx / "index.db", dim=8, use_sqlite_vec=False)
    store.init_schema()
    fp = str(fp_path)
    store.upsert_file(path=fp, mtime=1, content_hash="h")
    store.replace_chunks_for_file(
        fp,
        [
            dict(
                heading=None,
                line_start=1,
                line_end=1,
                text="t",
                tags=[],
                aliases=[],
                embedding=np.ones(8, dtype=np.float32),
            )
        ],
        updated=updated,
    )
    store.close()


def test_review_nudge_prints_only_when_overdue(tmp_path, monkeypatch):
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    _seed_index(tmp_path, "knowledge", "old", "2020-01-01")
    from rekol.cli_review import main

    res = CliRunner().invoke(main, ["--nudge"])
    assert res.exit_code == 0 and "due for review" in res.output


def test_review_confirm_stamps_last_confirmed_not_updated(tmp_path, monkeypatch):
    """Confirm bumps `last_confirmed` (verification), NOT `updated` (edit) — #87."""
    import frontmatter

    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    _seed_index(tmp_path, "knowledge", "old", "2020-01-01")
    target = tmp_path / "knowledge" / "old.md"
    from rekol.cli_review import main

    res = CliRunner().invoke(main, [], input="c\n")
    assert res.exit_code == 0, res.output
    post = frontmatter.load(str(target))
    assert str(post["last_confirmed"]) == dt.date.today().isoformat()
    # `updated` (the edit timestamp) must be untouched — a confirmation is not an edit.
    assert str(post["updated"]) == "2020-01-01"


def _legacy_index(home):
    import sqlite3

    idx = cache_dir_for(home)
    idx.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(idx / "index.db")
    con.execute(
        "CREATE TABLE files (path TEXT PRIMARY KEY, mtime INT, content_hash TEXT, indexed_at INT)"
    )
    con.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_path TEXT, heading TEXT, "
        "line_start INT, line_end INT, text TEXT, tags_json TEXT, aliases_json TEXT, embedding BLOB)"
    )
    con.commit()
    con.close()


def test_review_nudge_soft_fails_on_legacy_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    _legacy_index(tmp_path)
    from rekol.cli_review import main

    # --nudge runs inside the SessionEnd hook: must NOT crash it (exit 0).
    res = CliRunner().invoke(main, ["--nudge"])
    assert res.exit_code == 0
    # interactive review instructs and exits non-zero instead of crashing.
    res2 = CliRunner().invoke(main, [])
    assert res2.exit_code == 1
    assert "rekol index rebuild" in res2.output


def test_review_confirm_refuses_path_outside_memory_home(tmp_path, monkeypatch):
    from rekol.cli_review import _confirm
    from rekol.config import load_config

    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    (tmp_path / "always").mkdir()
    cfg = load_config()
    outside = tmp_path.parent / "evil.md"
    # Layer dir 'always' is exempt, but the path resolves outside memory_home.
    traversal = str(tmp_path / "always" / ".." / ".." / "evil.md")
    assert _confirm(traversal, cfg) is False
    assert not outside.exists()
