"""The SessionStart hook command cats REKOL.md when present, else MEMORY.md."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SNIPPET = REPO_ROOT / "hooks" / "sessionstart-snippet.json"


def _hook_command() -> str:
    data = json.loads(SNIPPET.read_text(encoding="utf-8"))
    # Snippet shape: {"hooks":{"SessionStart":[{"hooks":[{"command": "..."}]}]}}
    # Walk to the single command string regardless of nesting depth.
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "command" in node and isinstance(node["command"], str):
                found.append(node["command"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    assert len(found) == 1, f"expected exactly one command, got {len(found)}"
    return found[0]


def _run(command: str, home: Path) -> str:
    result = subprocess.run(
        ["bash", "-c", command],
        env={"REKOL_HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def test_prefers_rekol_md(tmp_path: Path) -> None:
    (tmp_path / "REKOL.md").write_text("REKOL CONTENT\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("LEGACY CONTENT\n", encoding="utf-8")
    out = _run(_hook_command(), tmp_path)
    assert "REKOL CONTENT" in out
    assert "LEGACY CONTENT" not in out


def test_falls_back_to_memory_md(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("LEGACY CONTENT\n", encoding="utf-8")
    out = _run(_hook_command(), tmp_path)
    assert "LEGACY CONTENT" in out


def test_no_index_file_prints_actionable_error(tmp_path: Path) -> None:
    # REKOL_HOME is set to a real dir, but neither index file exists.
    out = _run(_hook_command(), tmp_path)
    assert "not configured" not in out  # home IS configured
    assert "no REKOL.md found" in out
    assert "CONTENT" not in out  # nothing catted
