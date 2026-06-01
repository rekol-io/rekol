"""Tests for durable-memory overdue detection and the review command."""

import datetime as dt
from pathlib import Path

from click.testing import CliRunner

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
    idx = home / ".index"
    idx.mkdir(exist_ok=True)
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


def test_review_confirm_bumps_updated(tmp_path, monkeypatch):
    import frontmatter

    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    _seed_index(tmp_path, "knowledge", "old", "2020-01-01")
    target = tmp_path / "knowledge" / "old.md"
    from rekol.cli_review import main

    res = CliRunner().invoke(main, [], input="c\n")
    assert res.exit_code == 0, res.output
    assert str(frontmatter.load(str(target))["updated"]) == dt.date.today().isoformat()
