"""Tests for temporal ranking policy."""

import datetime as dt
from pathlib import Path

from rekol.ranking import apply_temporal_ranking

HOME = Path("/memhome")
TODAY = dt.date(2026, 6, 1)


def _hit(layer, name, score, **ts):
    return {"file_path": f"/memhome/{layer}/{name}.md", "score": score, **ts}


def _rank(hits, **kw):
    defaults = dict(
        memory_home=HOME,
        today=TODAY,
        recency_weight=0.03,
        recency_halflife_days=180,
        exempt_layers=["always", "knowledge"],
        exclude_invalidated=True,
        respect_valid_from=True,
        include_invalidated=False,
    )
    defaults.update(kw)
    return apply_temporal_ranking(hits, **defaults)


def test_invalidated_excluded_by_default():
    hits = [_hit("topics", "a", 0.9, invalidated_at="2026-03-01")]
    ranked, filtered = _rank(hits)
    assert ranked == [] and filtered == 1


def test_invalidated_included_but_below_live_when_flag_set():
    hits = [
        _hit("topics", "bad", 0.95, invalidated_at="2026-03-01"),
        _hit("topics", "live", 0.10),
    ]
    ranked, _ = _rank(hits, include_invalidated=True)
    assert [h["file_path"].split("/")[-1] for h in ranked] == ["live.md", "bad.md"]


def test_future_valid_from_filtered():
    hits = [_hit("topics", "future", 0.9, valid_from="2027-01-01")]
    ranked, filtered = _rank(hits)
    assert ranked == [] and filtered == 1


def test_recency_breaks_near_tie_for_timely_layer():
    hits = [
        _hit("topics", "old", 0.80, updated="2020-01-01"),
        _hit("topics", "new", 0.80, updated="2026-05-31"),
    ]
    ranked, _ = _rank(hits)
    assert ranked[0]["file_path"].endswith("new.md")


def test_strong_old_beats_weak_new():
    hits = [
        _hit("topics", "old", 0.90, updated="2019-01-01"),
        _hit("topics", "new", 0.50, updated="2026-05-31"),
    ]
    ranked, _ = _rank(hits)
    assert ranked[0]["file_path"].endswith("old.md")


def test_knowledge_layer_treated_as_always_current():
    # Durable knowledge/ hit (old) vs fresher topics/ hit at equal cosine. The
    # exempt hit gets the FULL un-decayed boost (always-current), so it is not
    # out-ranked by the fresher hit (whose boost is slightly decayed).
    hits = [
        _hit("knowledge", "durable", 0.80, updated="2019-01-01"),
        _hit("topics", "fresh", 0.80, updated="2026-05-31"),
    ]
    ranked, _ = _rank(hits)
    assert ranked[0]["file_path"].endswith("durable.md")


def test_cosine_score_preserved_and_final_score_added():
    hits = [_hit("topics", "a", 0.7, updated="2026-05-31")]
    ranked, _ = _rank(hits)
    assert ranked[0]["cosine_score"] == 0.7
    assert ranked[0]["final_score"] >= 0.7
