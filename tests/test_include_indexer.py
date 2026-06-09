"""Ongoing indexing of include_dirs: convert -> synthetic transcripts, incremental.

This is the riskiest surface (#63 scope 3). RED-first tests pin:
  * each included dir is converted into synthetic transcripts the session
    ingester can pick up,
  * the deny-list governs which files convert (a denied subtree never appears),
  * INCREMENTAL: an unchanged dir is skipped on the next run (no re-convert),
  * a NEW file in an already-included dir IS picked up on the next run,
  * a stable, collision-free prefix per included dir.
"""

from __future__ import annotations

from pathlib import Path

from rekol.config import load_config
from rekol.include_indexer import (
    IncludeIndexResult,
    dir_is_current,
    include_prefix_for,
    index_include_dirs,
    scoped_count_and_current,
    scoped_file_count,
)


def _write(path: Path, name: str, body: str = "knowledge\n") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    f = path / name
    f.write_text(body, encoding="utf-8")
    return f


def _make_cfg(tmp_path: Path, monkeypatch, include_dirs, include_deny=()):
    home = tmp_path / "memhome"
    home.mkdir(exist_ok=True)
    projects = tmp_path / "fake-projects"
    projects.mkdir(parents=True, exist_ok=True)
    newline = "\n"
    dirs_yaml = "".join(f"  - {d}{newline}" for d in include_dirs) or f"  []{newline}"
    deny_yaml = "".join(f"  - {d}{newline}" for d in include_deny) or f"  []{newline}"
    (home / "rekol.config.yaml").write_text(
        f"claude_projects_dir: {projects}\n"
        "embedding_model: test-hashing\n"
        f"include_dirs:\n{dirs_yaml}"
        f"include_deny:\n{deny_yaml}",
        encoding="utf-8",
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    return load_config(), projects


def test_converts_included_dir_to_transcripts(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    _write(src / "papers", "hypothesis.md", "the tabula rasa thesis\n")
    cfg, projects = _make_cfg(tmp_path, monkeypatch, [src])
    result = index_include_dirs(cfg)
    assert isinstance(result, IncludeIndexResult)
    prefix_dir = projects / include_prefix_for(src)
    written = list(prefix_dir.glob("*.jsonl"))
    assert written, "expected synthetic transcripts under the include prefix"
    body = "\n".join(p.read_text(encoding="utf-8") for p in written)
    assert "tabula rasa" in body


def test_deny_list_governs_conversion(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    _write(src / "keep", "good.md", "KEEPER\n")
    _write(src / "old-projects", "stale.md", "DROPME\n")
    cfg, projects = _make_cfg(tmp_path, monkeypatch, [src], include_deny=["old-projects"])
    index_include_dirs(cfg)
    body = "\n".join(
        p.read_text(encoding="utf-8") for p in (projects / include_prefix_for(src)).glob("*.jsonl")
    )
    assert "KEEPER" in body
    assert "DROPME" not in body


def test_junk_never_converted(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "repo"
    _write(src / "docs", "real.md", "REAL\n")
    _write(src / "node_modules" / "pkg", "junk.md", "JUNK\n")
    cfg, projects = _make_cfg(tmp_path, monkeypatch, [src])
    index_include_dirs(cfg)
    body = "\n".join(
        p.read_text(encoding="utf-8") for p in (projects / include_prefix_for(src)).glob("*.jsonl")
    )
    assert "REAL" in body
    assert "JUNK" not in body


def test_incremental_skips_unchanged_dir(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    _write(src / "papers", "a.md", "alpha\n")
    cfg, _projects = _make_cfg(tmp_path, monkeypatch, [src])
    first = index_include_dirs(cfg)
    assert src.resolve() in first.dirs_converted
    # No changes between runs -> the dir must be SKIPPED, not re-converted.
    second = index_include_dirs(cfg)
    assert src.resolve() in second.dirs_skipped_unchanged
    assert src.resolve() not in second.dirs_converted


def test_new_file_picked_up_next_run(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    _write(src / "papers", "a.md", "alpha\n")
    cfg, projects = _make_cfg(tmp_path, monkeypatch, [src])
    index_include_dirs(cfg)
    # A dir included once -> a NEW file is picked up on the next run.
    _write(src / "papers", "b.md", "BRAVO_NEW\n")
    second = index_include_dirs(cfg)
    assert src.resolve() in second.dirs_converted
    body = "\n".join(
        p.read_text(encoding="utf-8") for p in (projects / include_prefix_for(src)).glob("*.jsonl")
    )
    assert "BRAVO_NEW" in body


def test_removed_file_drops_out_next_run(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    a = _write(src / "papers", "a.md", "alpha\n")
    _write(src / "papers", "b.md", "BRAVO\n")
    cfg, projects = _make_cfg(tmp_path, monkeypatch, [src])
    index_include_dirs(cfg)
    a.unlink()
    second = index_include_dirs(cfg)
    assert src.resolve() in second.dirs_converted
    body = "\n".join(
        p.read_text(encoding="utf-8") for p in (projects / include_prefix_for(src)).glob("*.jsonl")
    )
    assert "alpha" not in body
    assert "BRAVO" in body


def test_prefix_is_stable_and_distinct(tmp_path: Path) -> None:
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    assert include_prefix_for(a) == include_prefix_for(a)  # stable
    assert include_prefix_for(a) != include_prefix_for(b)  # distinct
    assert include_prefix_for(a).startswith("rekol-include-")


def test_no_include_dirs_is_noop(tmp_path: Path, monkeypatch) -> None:
    cfg, projects = _make_cfg(tmp_path, monkeypatch, [])
    result = index_include_dirs(cfg)
    assert result.dirs_converted == []
    assert result.dirs_skipped_unchanged == []


def test_missing_include_dir_is_skipped_not_fatal(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "does-not-exist"
    cfg, _projects = _make_cfg(tmp_path, monkeypatch, [missing])
    # A configured-but-absent dir must not crash the run.
    result = index_include_dirs(cfg)
    assert missing.resolve() not in result.dirs_converted


def test_scoped_count_and_current_matches_separate_helpers(tmp_path: Path, monkeypatch) -> None:
    """#76: the single-walk helper returns the same (count, is_current) as the separate
    scoped_file_count + dir_is_current — before and after an index run (current flips).
    Pinning the equivalence guards the coverage double-scan TOCTOU fix."""
    src = tmp_path / "notes"
    _write(src / "a", "one.md")
    _write(src / "b", "two.md")
    cfg, _ = _make_cfg(tmp_path, monkeypatch, [src])

    # Before any conversion: count matches, dir is NOT current (no marker yet).
    count, current = scoped_count_and_current(cfg, src)
    assert count == scoped_file_count(cfg, src) == 2
    assert current is dir_is_current(cfg, src) is False

    # After indexing: marker written → current True; count unchanged.
    index_include_dirs(cfg)
    count2, current2 = scoped_count_and_current(cfg, src)
    assert count2 == scoped_file_count(cfg, src) == 2
    assert current2 is dir_is_current(cfg, src) is True

    # A missing dir is (0, False).
    assert scoped_count_and_current(cfg, tmp_path / "nope") == (0, False)
