"""Find durable (exempt-layer) memories overdue for re-confirmation.

Exempt layers (``always/``, ``knowledge/``) are treated as always-current by
ranking, so they never decay out of recall. To keep a wrong-but-durable fact
from sitting at the top forever, this surfaces ones not confirmed within the
configured interval. Confirmation age keys off ``last_confirmed`` (#87) — the
explicit "I verified this still holds" stamp — falling back to ``updated``/
``created`` for memories never confirmed since the field shipped (forward-only).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from rekol.ranking import _as_date, _layer_of


def find_overdue(
    rows: list[dict[str, Any]],
    *,
    memory_home: Path,
    exempt_layers: list[str],
    interval_days: int,
    today: dt.date,
) -> list[dict[str, Any]]:
    """Return overdue durable memories as ``[{file_path, updated, last_confirmed, age_days}]``.

    ``rows`` is ``[{file_path, updated, created, last_confirmed}]``. A file is
    durable when its layer is in ``exempt_layers``; overdue when its confirmation
    reference — ``last_confirmed`` (fallback ``updated``/``created``) — is older
    than ``interval_days``, or absent. Most-overdue first (a memory with no usable
    date is treated as the most overdue).
    """
    exempt = set(exempt_layers)
    out: list[dict[str, Any]] = []
    for row in rows:
        if _layer_of(row["file_path"], memory_home) not in exempt:
            continue
        ref = _as_date(row.get("last_confirmed") or row.get("updated") or row.get("created"))
        entry = {
            "file_path": row["file_path"],
            "updated": row.get("updated"),
            "last_confirmed": row.get("last_confirmed"),
        }
        if ref is None:
            out.append({**entry, "age_days": None})
        elif (today - ref).days > interval_days:
            out.append({**entry, "age_days": (today - ref).days})
    out.sort(key=lambda x: -(x["age_days"] if x["age_days"] is not None else 10**9))
    return out
