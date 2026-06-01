"""Temporal ranking policy for curated memory hits.

Pure functions over the hit dicts returned by :meth:`IndexStore.search` — kept
out of the storage layer so the store has no config dependency and this policy
is unit-testable in isolation.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Any

# Large enough to push an included-invalidated hit below every live hit
# regardless of its cosine score or recency boost.
_INVALIDATED_PENALTY = 1000.0


def _layer_of(file_path: str, memory_home: Path) -> str | None:
    """Top-level layer dir of a memory file (e.g. ``"knowledge"``), or None."""
    try:
        rel = Path(file_path).relative_to(memory_home)
    except ValueError:
        return None
    return rel.parts[0] if len(rel.parts) > 1 else None


def _as_date(value: object) -> dt.date | None:
    """Best-effort date-granularity parse of an ISO string; bad/empty -> None."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def apply_temporal_ranking(
    hits: list[dict[str, Any]],
    *,
    memory_home: Path,
    today: dt.date,
    recency_weight: float,
    recency_halflife_days: float,
    exempt_layers: list[str],
    exclude_invalidated: bool,
    respect_valid_from: bool,
    include_invalidated: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Filter and re-rank curated hits temporally.

    Returns ``(ranked_hits, filtered_count)``. ``filtered_count`` counts hits
    removed by the invalidation / valid_from filters, used to suppress a false
    'no memory — consider capturing' hint when matches exist but are all
    invalidated or not-yet-valid.
    """
    exempt = set(exempt_layers)
    half = max(1.0, float(recency_halflife_days))
    kept: list[dict[str, Any]] = []
    filtered_count = 0

    for raw in hits:
        h = dict(raw)
        h["cosine_score"] = float(h.get("cosine_score", h.get("score", 0.0)))
        invalidated = bool(h.get("invalidated_at"))

        if respect_valid_from:
            valid_from = _as_date(h.get("valid_from") or h.get("created"))
            if valid_from is not None and valid_from > today:
                filtered_count += 1
                continue

        if invalidated and exclude_invalidated and not include_invalidated:
            filtered_count += 1
            continue

        if _layer_of(h["file_path"], memory_home) in exempt:
            boost = recency_weight  # time-insensitive layer: full, un-decayed boost
        else:
            ref = _as_date(h.get("updated") or h.get("created"))
            if ref is not None:
                age_days = max(0, (today - ref).days)
                boost = recency_weight * math.exp(-age_days / half)
            else:
                boost = 0.0

        final = h["cosine_score"] + boost
        if invalidated:  # only reachable under include_invalidated
            final -= _INVALIDATED_PENALTY
        h["final_score"] = final
        kept.append(h)

    kept.sort(key=lambda x: -x["final_score"])
    return kept, filtered_count
