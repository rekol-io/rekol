"""scope: frontmatter field — defaults to 'private', any value is preserved (unread, unvalidated in v0.1)."""

from __future__ import annotations

from pathlib import Path

from rekol.model import parse_file

_BASE = """---
name: t
description: d
type: topic
{scope_line}---

body
"""


def _write(tmp_path: Path, scope_line: str) -> Path:
    p = tmp_path / "t.md"
    p.write_text(_BASE.format(scope_line=scope_line), encoding="utf-8")
    return p


def test_scope_defaults_to_private(tmp_path: Path) -> None:
    mf = parse_file(_write(tmp_path, ""))
    assert mf.scope == "private"


def test_scope_explicit_private(tmp_path: Path) -> None:
    mf = parse_file(_write(tmp_path, "scope: private\n"))
    assert mf.scope == "private"


def test_scope_shared_preserved(tmp_path: Path) -> None:
    mf = parse_file(_write(tmp_path, "scope: shared\n"))
    assert mf.scope == "shared"


def test_unknown_scope_is_preserved_not_rejected(tmp_path: Path) -> None:
    # v0.1 reserves but does NOT read or validate scope. A file that already
    # uses scope: informally (e.g. 'work') must still parse and index — never
    # be silently dropped from the index by a ValidationError.
    mf = parse_file(_write(tmp_path, "scope: work\n"))
    assert mf.scope == "work"
