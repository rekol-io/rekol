"""Tests for the #87 confidence metadata: confirm / flag-suspect / surfacing.

Hermetic: ``embedding_model: test-hashing`` so the reindex inside each command
uses the deterministic HashingEmbedder (no ML model / network); the conftest
autouse fixture isolates the cache/archive env.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import frontmatter
from click.testing import CliRunner

from rekol.cli_search import _confidence_tag
from rekol.model import parse_file
from rekol.review import find_overdue


def _seed(home: Path, *, layer: str = "knowledge", name: str = "x", **fm_extra: str) -> Path:
    """Write a memory file + a test-hashing config; return the file path."""
    (home / layer).mkdir(parents=True, exist_ok=True)
    (home / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")
    fm_lines = "".join(f"{k}: {v}\n" for k, v in fm_extra.items())
    path = home / layer / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: d\ntype: {layer}\n{fm_lines}---\nbody about deploys\n"
    )
    return path


# --------------------------------------------------------------------------- #
# rekol confirm
# --------------------------------------------------------------------------- #


def test_confirm_stamps_last_confirmed_without_touching_updated(tmp_path, monkeypatch):
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    path = _seed(tmp_path, name="deploy", updated="2020-01-01")
    from rekol.cli_confidence import confirm

    res = CliRunner().invoke(confirm, [str(path)])
    assert res.exit_code == 0, res.output
    post = frontmatter.load(str(path))
    assert str(post["last_confirmed"]) == dt.date.today().isoformat()
    assert str(post["updated"]) == "2020-01-01", "confirm must not masquerade as an edit"


def test_confirm_clears_a_prior_suspect_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    path = _seed(
        tmp_path,
        name="deploy",
        suspected_at="2026-06-01T10:00:00-07:00",
        suspect_reason="path moved",
    )
    from rekol.cli_confidence import confirm

    res = CliRunner().invoke(confirm, [str(path)])
    assert res.exit_code == 0, res.output
    post = frontmatter.load(str(path))
    assert "suspected_at" not in post.metadata
    assert "suspect_reason" not in post.metadata


def test_confirm_preserves_unknown_frontmatter_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    path = _seed(tmp_path, name="deploy", tags="[a, b]", custom_key="keepme")
    from rekol.cli_confidence import confirm

    CliRunner().invoke(confirm, [str(path)])
    post = frontmatter.load(str(path))
    assert post.metadata.get("custom_key") == "keepme", "round-trip must preserve other keys"


def test_confirm_refuses_path_outside_memory_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("REKOL_HOME", str(home))
    (home / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: x\ndescription: d\ntype: knowledge\n---\nb\n")
    from rekol.cli_confidence import confirm

    res = CliRunner().invoke(confirm, [str(outside)])
    assert res.exit_code != 0
    assert "not under MEMORY_HOME" in res.output


# --------------------------------------------------------------------------- #
# rekol flag-suspect
# --------------------------------------------------------------------------- #


def test_flag_suspect_stamps_suspected_at_and_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    path = _seed(tmp_path, name="deploy")
    from rekol.cli_confidence import flag_suspect

    res = CliRunner().invoke(flag_suspect, [str(path), "--reason", "cluster decommissioned"])
    assert res.exit_code == 0, res.output
    post = frontmatter.load(str(path))
    assert post.metadata.get("suspected_at"), "suspected_at must be stamped"
    assert post.metadata.get("suspect_reason") == "cluster decommissioned"


def test_flag_suspect_refuses_when_already_invalidated(tmp_path, monkeypatch):
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    path = _seed(tmp_path, name="deploy", invalidated_at="2026-05-01T00:00:00-07:00")
    from rekol.cli_confidence import flag_suspect

    res = CliRunner().invoke(flag_suspect, [str(path), "--reason", "x"])
    assert res.exit_code != 0
    assert "already invalidated" in res.output


# --------------------------------------------------------------------------- #
# Surfacing — _confidence_tag (exclude-but-explain, never silent)
# --------------------------------------------------------------------------- #


def test_confidence_tag_unconfirmed_by_default():
    assert _confidence_tag({}) == " · unconfirmed"


def test_confidence_tag_shows_confirmed_relative_age():
    tag = _confidence_tag({"last_confirmed": "2026-01-01", "confirmed_rel": "5 months ago"})
    assert tag == " · confirmed 5 months ago"


def test_confidence_tag_flags_suspect_with_reason():
    tag = _confidence_tag({"suspected_at": "2026-06-01T10:00:00", "suspect_reason": "moved"})
    assert "⚠ suspected (since 2026-06-01T10:00:00 — moved)" in tag


def test_confidence_tag_suppressed_for_invalidated():
    # Invalidated hits already carry [INVALIDATED]; don't double up.
    assert _confidence_tag({"invalidated_at": "2026-05-01", "last_confirmed": None}) == ""


# --------------------------------------------------------------------------- #
# Overdue keys off last_confirmed, not updated
# --------------------------------------------------------------------------- #


def test_find_overdue_uses_last_confirmed_over_updated(tmp_path):
    today = dt.date(2026, 6, 10)
    rows = [
        # Edited long ago but confirmed YESTERDAY → NOT overdue.
        {
            "file_path": str(tmp_path / "knowledge" / "fresh.md"),
            "updated": "2020-01-01",
            "created": "2020-01-01",
            "last_confirmed": "2026-06-09",
        },
        # Edited recently but NEVER confirmed, and old → overdue (unverified).
        {
            "file_path": str(tmp_path / "knowledge" / "unverified.md"),
            "updated": "2024-01-01",
            "created": "2024-01-01",
            "last_confirmed": None,
        },
    ]
    overdue = find_overdue(
        rows,
        memory_home=tmp_path,
        exempt_layers=["knowledge"],
        interval_days=90,
        today=today,
    )
    paths = {Path(o["file_path"]).name for o in overdue}
    assert "unverified.md" in paths
    assert "fresh.md" not in paths, "a recently-confirmed memory is not overdue"


# --------------------------------------------------------------------------- #
# model parse
# --------------------------------------------------------------------------- #


def test_parse_file_reads_confidence_fields(tmp_path):
    path = _seed(
        tmp_path,
        name="deploy",
        last_confirmed="2026-06-01",
        suspected_at="2026-06-05T09:00:00-07:00",
        suspect_reason="value changed",
    )
    mf = parse_file(path)
    assert mf.last_confirmed == "2026-06-01"
    assert mf.suspected_at == "2026-06-05T09:00:00-07:00"
    assert mf.suspect_reason == "value changed"
    assert mf.is_suspect is True
