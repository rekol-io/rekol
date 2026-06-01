"""Tests for durable-memory overdue detection and the review command."""

import datetime as dt
from pathlib import Path

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
