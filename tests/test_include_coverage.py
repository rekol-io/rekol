"""Coverage report + %: indexed-vs-discoverable across include_dirs (T8 #63).

The spec close line: "Z of ~Y discoverable files indexed (~C%)". These RED
tests pin the formula: discoverable = in-scope files on disk; indexed = in-scope
files whose dir has an up-to-date conversion marker; pct = indexed/discoverable.
"""

from __future__ import annotations

from pathlib import Path

from rekol.config import load_config
from rekol.include_coverage import CoverageReport, compute_coverage
from rekol.include_indexer import index_include_dirs


def _write(path: Path, name: str, body: str = "x\n") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    f = path / name
    f.write_text(body, encoding="utf-8")
    return f


def _cfg(tmp_path: Path, monkeypatch, include_dirs, include_deny=()):
    home = tmp_path / "memhome"
    home.mkdir(exist_ok=True)
    projects = tmp_path / "fake-projects"
    projects.mkdir(parents=True, exist_ok=True)
    nl = "\n"
    dirs_yaml = "".join(f"  - {d}{nl}" for d in include_dirs) or f"  []{nl}"
    deny_yaml = "".join(f"  - {d}{nl}" for d in include_deny) or f"  []{nl}"
    (home / "rekol.config.yaml").write_text(
        f"claude_projects_dir: {projects}\n"
        "embedding_model: test-hashing\n"
        f"include_dirs:\n{dirs_yaml}"
        f"include_deny:\n{deny_yaml}",
        encoding="utf-8",
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    return load_config()


def test_no_include_dirs_zero_coverage(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, monkeypatch, [])
    report = compute_coverage(cfg)
    assert isinstance(report, CoverageReport)
    assert report.discoverable == 0
    assert report.indexed == 0
    # No discoverable files -> percent is defined as 0, never a divide-by-zero.
    assert report.percent == 0.0


def test_discoverable_counts_in_scope_files(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    _write(src / "a", "1.md")
    _write(src / "a", "2.md")
    _write(src / "b", "3.md")
    cfg = _cfg(tmp_path, monkeypatch, [src])
    report = compute_coverage(cfg)
    assert report.discoverable == 3
    # Nothing converted yet -> indexed 0.
    assert report.indexed == 0


def test_deny_and_junk_excluded_from_discoverable(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    _write(src / "keep", "good.md")
    _write(src / "skip", "bad.md")
    _write(src / "node_modules", "junk.md")
    cfg = _cfg(tmp_path, monkeypatch, [src], include_deny=["skip"])
    report = compute_coverage(cfg)
    assert report.discoverable == 1  # only keep/good.md


def test_full_coverage_after_index(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    _write(src / "a", "1.md")
    _write(src / "a", "2.md")
    cfg = _cfg(tmp_path, monkeypatch, [src])
    index_include_dirs(cfg)  # converts -> marker written
    report = compute_coverage(cfg)
    assert report.discoverable == 2
    assert report.indexed == 2
    assert report.percent == 100.0


def test_partial_coverage_when_one_dir_unconverted(tmp_path: Path, monkeypatch) -> None:
    converted = tmp_path / "converted"
    _write(converted, "a.md")
    _write(converted, "b.md")
    pending = tmp_path / "pending"
    _write(pending, "c.md")
    _write(pending, "d.md")
    cfg = _cfg(tmp_path, monkeypatch, [converted, pending])
    # Convert only the first dir, then add the second to scope's marker gap by
    # NOT converting it (simulate a stale run): index then add new files.
    index_include_dirs(cfg)  # converts BOTH (both new)
    # Add a new file to `pending` only -> its marker is now stale, so its files
    # are discoverable-but-not-current-indexed.
    _write(pending, "e.md")
    report = compute_coverage(cfg)
    assert report.discoverable == 5  # a,b,c,d,e
    assert report.indexed == 2  # only `converted` dir is current (a,b)
    assert 0 < report.percent < 100


def test_report_summary_line_shape(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "research"
    _write(src / "a", "1.md")
    cfg = _cfg(tmp_path, monkeypatch, [src])
    index_include_dirs(cfg)
    report = compute_coverage(cfg)
    line = report.summary()
    # Matches the spec close line shape "Z of ~Y discoverable files indexed (~C%)".
    assert "1 of" in line and "discoverable" in line and "%" in line
