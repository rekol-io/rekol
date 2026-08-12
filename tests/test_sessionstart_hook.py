"""The SessionStart hook command cats REKOL.md when present, else MEMORY.md."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SNIPPET = REPO_ROOT / "hooks" / "sessionstart-snippet.json"


def _hook_command() -> str:
    data = json.loads(SNIPPET.read_text(encoding="utf-8"))
    # Snippet shape: {"hooks":{"SessionStart":[{"hooks":[{"command": "..."}]}, ...]}}
    # The block now has multiple handlers (memory loader + the #123 coverage
    # banner); these tests exercise the memory-loader command — the one that cats
    # the index file. Walk to every command, then select that one.
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
    loaders = [command for command in found if "REKOL.md" in command]
    assert len(loaders) == 1, f"expected exactly one memory-loader command, got {len(loaders)}"
    return loaders[0]


def _rekol_executable() -> Path | None:
    """A real rekol console script, or None."""
    candidate = Path(sys.executable).parent / "rekol"
    if candidate.exists():
        return candidate
    found = shutil.which("rekol")
    return Path(found) if found else None


def _render(command: str) -> str:
    """Substitute @REKOL@ the way install.sh does.

    The snippet ships with a literal ``@REKOL@`` placeholder. These tests used to
    run the command STRAIGHT FROM THE REPO, so the trailing handler was
    ``"@REKOL@" _hook session-confidence`` — exit 127, ``bash: @REKOL@: command
    not found`` — masked by the hook's own ``|| true`` and then discarded because
    ``_run`` returned only stdout. An unrendered template plus a swallowed exit
    code is structurally blind to the entire #159 class (#170).
    """
    exe = _rekol_executable()
    if exe is None:
        pytest.skip("no rekol console script available to render @REKOL@ against")
    return command.replace("@REKOL@", str(exe))


def _run(command: str, home: Path) -> subprocess.CompletedProcess[str]:
    """Run a RENDERED hook command. Returns the whole result — callers must check
    ``returncode``; returning only stdout is what hid the placeholder failure."""
    return subprocess.run(
        ["bash", "-c", _render(command)],
        env={"REKOL_HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_prefers_rekol_md(tmp_path: Path) -> None:
    (tmp_path / "REKOL.md").write_text("REKOL CONTENT\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("LEGACY CONTENT\n", encoding="utf-8")
    result = _run(_hook_command(), tmp_path)
    assert result.returncode == 0, result.stderr
    assert "REKOL CONTENT" in result.stdout
    assert "LEGACY CONTENT" not in result.stdout


def test_falls_back_to_memory_md(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("LEGACY CONTENT\n", encoding="utf-8")
    result = _run(_hook_command(), tmp_path)
    assert result.returncode == 0, result.stderr
    assert "LEGACY CONTENT" in result.stdout


def test_no_index_file_prints_actionable_error(tmp_path: Path) -> None:
    # REKOL_HOME is set to a real dir, but neither index file exists.
    result = _run(_hook_command(), tmp_path)
    assert result.returncode == 0, result.stderr
    assert "not configured" not in result.stdout  # home IS configured
    assert "no REKOL.md found" in result.stdout
    assert "CONTENT" not in result.stdout  # nothing catted


def test_every_segment_of_the_hook_runs_unmasked(tmp_path: Path) -> None:
    """#170: the command's own `2>/dev/null || true` hides a broken handler.

    The memory loader is a compound command whose LAST segment invokes a rekol
    handler. With the mask in place the whole thing exits 0 no matter what, which
    is correct for production (a hook must never break a session) and useless as a
    test. Strip the mask and require a real exit 0 — that is the only form of this
    assertion that could have caught #159.
    """
    (tmp_path / "REKOL.md").write_text("REKOL CONTENT\n", encoding="utf-8")
    command = _render(_hook_command())

    unmasked = command
    for mask in (" 2>/dev/null || true", " || true"):
        if unmasked.endswith(mask):
            unmasked = unmasked[: -len(mask)]
    assert unmasked, "stripping emptied the command — the assertion would be vacuous"
    assert "|| true" not in unmasked, f"mask survived the strip: {unmasked}"
    assert unmasked != command, "no mask was present to strip — check the snippet shape"

    result = subprocess.run(
        ["bash", "-c", unmasked],
        env={"REKOL_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"unmasked hook failed ({result.returncode}): {result.stderr}"


def test_snippet_placeholder_would_not_run_unrendered() -> None:
    """Guard the guard: prove the placeholder really is unrunnable, so nobody
    'simplifies' _render away and reintroduces the blindness."""
    raw = _hook_command()
    assert "@REKOL@" in raw, "snippet no longer uses the placeholder — update this test"
    result = subprocess.run(
        ["bash", "-c", raw.replace(" 2>/dev/null || true", "")],
        env={"REKOL_HOME": "/tmp", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "an unrendered @REKOL@ must NOT succeed"
