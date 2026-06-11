"""Tests for ``rekol _hook session-confidence`` (#87 item 4).

The footer rides on the SessionStart injection and must NEVER break it: any error
prints nothing and exits 0. It flags always-on memories (the proactively-volunteered
ones) that are suspect / overdue / never-confirmed, severity-ordered and capped.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from click.testing import CliRunner

from rekol.cli_hooks import _always_confidence_lines, session_confidence


def _seed(home: Path, monkeypatch, *, interval: int = 30) -> None:
    (home / "always").mkdir(parents=True, exist_ok=True)
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\ntemporal_confirm_interval_days: {interval}\n"
    )
    monkeypatch.setenv("REKOL_HOME", str(home))


def _always(home: Path, name: str, **fm: str) -> None:
    extra = "".join(f"{k}: {v}\n" for k, v in fm.items())
    (home / "always" / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: d\ntype: always\n{extra}---\n\nbody\n"
    )


def test_never_confirmed_always_file_is_flagged(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _always(tmp_path, "identity")  # no last_confirmed
    lines = _always_confidence_lines()
    assert any("always/identity.md" in ln and "never confirmed" in ln for ln in lines)


def test_suspect_always_file_is_flagged_first_with_reason(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _always(tmp_path, "deploy", suspected_at="2026-06-01T10:00:00-07:00", suspect_reason="moved")
    _always(tmp_path, "identity")  # never confirmed
    lines = _always_confidence_lines()
    # Suspect sorts ahead of never-confirmed.
    assert "always/deploy.md" in lines[0]
    assert "⚠ suspected" in lines[0] and "moved" in lines[0]


def test_overdue_confirmation_is_flagged(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, interval=30)
    old = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    _always(tmp_path, "stale", last_confirmed=old)
    lines = _always_confidence_lines()
    assert any("always/stale.md" in ln and "overdue" in ln for ln in lines)


def test_recently_confirmed_file_is_not_flagged(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, interval=30)
    fresh = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    _always(tmp_path, "fresh", last_confirmed=fresh)
    assert _always_confidence_lines() == []


def test_invalidated_always_file_is_skipped(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _always(tmp_path, "dead", invalidated_at="2026-05-01T00:00:00-07:00")
    assert _always_confidence_lines() == []


def test_command_prints_footer_when_flagged(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _always(tmp_path, "identity")
    result = CliRunner().invoke(session_confidence, [])
    assert result.exit_code == 0
    assert "rekol confidence" in result.output
    assert "always/identity.md" in result.output


def test_command_prints_nothing_when_all_confirmed(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, interval=30)
    _always(tmp_path, "ok", last_confirmed=dt.date.today().isoformat())
    result = CliRunner().invoke(session_confidence, [])
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_command_soft_fails_to_exit_zero_when_home_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("REKOL_HOME", raising=False)
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    result = CliRunner().invoke(session_confidence, [])
    # Never raise out of a hook — at worst, no output.
    assert result.exit_code == 0


def test_footer_is_capped_with_overflow_count(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    for i in range(9):  # all never-confirmed → 9 flags, cap is 6
        _always(tmp_path, f"file{i}")
    result = CliRunner().invoke(session_confidence, [])
    assert result.exit_code == 0
    assert "…and 3 more" in result.output
    # Exactly 6 bullet lines shown.
    assert sum(1 for ln in result.output.splitlines() if ln.startswith("  · ")) == 6
