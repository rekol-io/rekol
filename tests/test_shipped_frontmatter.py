"""Every file rekol SHIPS with YAML frontmatter must actually parse (#175).

This bug has now shipped three times, always the same way — an unquoted ``: ``
inside a plain YAML scalar:

1. ``feedback/rekol-frontmatter-style.md`` in a live store — the memory file whose
   entire subject was "don't get silently skipped by the indexer", silently
   skipped by the indexer for exactly this reason.
2. ``skill/rekol-bootstrap/skill.md`` — shipped by us, copied to every install.
3. (this test exists so there is no third recurrence in a file we control)

Claude Code's own parser is more forgiving than ``yaml.safe_load``, so a
malformed skill still loads and the damage is invisible until something of ours
parses strictly — which is precisely what skills-drift detection would add.
``description`` is also the string Claude Code relevance-matches on, so a
malformed one is not cosmetic.

Scoped to files in THIS REPO. A user's own memory files are their business and
are reported by ``rekol doctor`` at runtime; these are the ones we hand out.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

SHIPPED_WITH_FRONTMATTER = sorted(
    [
        *REPO_ROOT.glob("skill/*/*.md"),
        *REPO_ROOT.glob("src/rekol/template/**/*.md"),
    ]
)


def _frontmatter(path: Path) -> str | None:
    """The raw YAML block, or None when the file has no frontmatter."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    return parts[1] if len(parts) >= 3 else None


def test_there_are_shipped_files_to_check() -> None:
    """Guard the guard: an empty glob would make every test below vacuous."""
    assert SHIPPED_WITH_FRONTMATTER, "no shipped .md files found — the globs are wrong"


@pytest.mark.parametrize("path", SHIPPED_WITH_FRONTMATTER, ids=lambda p: str(p.name))
def test_shipped_frontmatter_parses(path: Path) -> None:
    raw = _frontmatter(path)
    if raw is None:
        return  # no frontmatter block is fine; a malformed one is not
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        rel = path.relative_to(REPO_ROOT)
        raise AssertionError(
            f"{rel}: frontmatter is not valid YAML — {str(exc).splitlines()[0]}\n"
            f"Most likely an unquoted ': ' inside a plain scalar; wrap the value in quotes."
        ) from exc
    assert isinstance(loaded, dict), f"{path.relative_to(REPO_ROOT)}: frontmatter is not a mapping"


@pytest.mark.parametrize(
    "path",
    [p for p in SHIPPED_WITH_FRONTMATTER if p.parent.parent.name == "skill"],
    ids=lambda p: str(p.parent.name),
)
def test_shipped_skills_declare_name_and_description(path: Path) -> None:
    """Claude Code matches on `description`; a skill without one cannot surface."""
    raw = _frontmatter(path)
    assert raw is not None, f"{path.relative_to(REPO_ROOT)}: a skill needs frontmatter"
    meta = yaml.safe_load(raw)
    for field in ("name", "description"):
        assert meta.get(field), f"{path.relative_to(REPO_ROOT)}: missing/empty '{field}'"


# --------------------------------- #176 --------------------------------------


@pytest.mark.parametrize("script", ["install.sh", "uninstall.sh"])
@pytest.mark.parametrize(
    "env_value",
    [None, "", "   ", "/tmp/relocated-claude", "/tmp/dir with spaces/.claude"],
    ids=["unset", "empty", "whitespace", "path", "path-with-spaces"],
)
def test_shell_and_python_agree_on_the_claude_config_dir(
    script: str, env_value: str | None, tmp_path: Path, monkeypatch
) -> None:
    """`install.sh`/`uninstall.sh` resolve CLAUDE_CONFIG_DIR in bash; the package
    resolves it in Python. Both hardcoded ``$HOME/.claude`` until #176.

    The rule is duplicated on purpose — uninstall must resolve it with the venv
    already deleted, and SETTINGS_JSON is needed long before the venv exists — so
    this test is what stops the two copies drifting. A mismatch means the
    installer writes hooks and skills where the runtime does not look, and both
    sides report success.
    """
    import os
    import subprocess

    fake_home = str(tmp_path / "home")
    env = {"HOME": fake_home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if env_value is not None:
        env["CLAUDE_CONFIG_DIR"] = env_value

    # Ask the SHELL script for its answer by sourcing only its resolution block.
    src = (REPO_ROOT / script).read_text(encoding="utf-8")
    start = src.index('if [[ -z "${CLAUDE_CONFIG_DIR:-}"')
    end = src.index("readonly CLAUDE_CONFIG_HOME", start)
    block = src[start:end]
    shell_answer = subprocess.run(
        ["bash", "-c", block + '\nprintf "%s" "$CLAUDE_CONFIG_HOME"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # Ask PYTHON for its answer under the same environment.
    monkeypatch.setenv("HOME", fake_home)
    if env_value is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", env_value)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(fake_home)))
    from rekol.config import resolve_claude_config_dir

    assert shell_answer == str(resolve_claude_config_dir()), (
        f"{script} and resolve_claude_config_dir() disagree for "
        f"CLAUDE_CONFIG_DIR={env_value!r}: shell={shell_answer!r} "
        f"python={resolve_claude_config_dir()!r}"
    )
