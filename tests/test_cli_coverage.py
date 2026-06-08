"""`rekol coverage`: standalone include-scope coverage surface (T8 #63).

A focused command the onboarding adaptive-close calls to print the
"Z of ~Y discoverable files indexed (~C%)" line, separate from the broader
``doctor`` health report.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from rekol.cli_coverage import main as coverage_main


def _home(tmp_path: Path, monkeypatch, include_dirs) -> Path:
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    nl = "\n"
    dirs_yaml = "".join(f"  - {d}{nl}" for d in include_dirs) or f"  []{nl}"
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\nclaude_projects_dir: {projects}\n"
        f"include_dirs:\n{dirs_yaml}include_deny:\n  []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    return home


def test_coverage_reports_line(tmp_path: Path, monkeypatch) -> None:
    from rekol.config import load_config
    from rekol.include_indexer import index_include_dirs

    research = tmp_path / "research"
    (research / "p").mkdir(parents=True)
    (research / "p" / "a.md").write_text("alpha\n", encoding="utf-8")
    _home(tmp_path, monkeypatch, [research])
    index_include_dirs(load_config())

    result = CliRunner().invoke(coverage_main, [])
    assert result.exit_code == 0, result.output
    assert "1 of" in result.output
    assert "discoverable" in result.output
    assert "%" in result.output


def test_coverage_handles_no_include_dirs(tmp_path: Path, monkeypatch) -> None:
    _home(tmp_path, monkeypatch, [])
    result = CliRunner().invoke(coverage_main, [])
    assert result.exit_code == 0, result.output
    # No include-scope configured: a clear, non-error message (not a crash).
    assert "no include" in result.output.lower() or "0 of" in result.output.lower()
