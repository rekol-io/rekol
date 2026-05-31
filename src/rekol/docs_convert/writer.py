"""Write synthetic transcript rows to one .jsonl file per session.

Output layout: ``<target_dir>/<prefix>/<safe-session-name>.jsonl``. The whole
``<prefix>`` dir is recreated on each run (clean overwrite) so a removed source
folder does not leave a stale .jsonl behind that the ingester would keep.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(session_name: str) -> str:
    """Turn a session name into a filesystem-safe stem (collapse unsafe runs to '-')."""
    return _UNSAFE.sub("-", session_name)


def write_sessions(
    target_dir: Path, prefix: str, rows_by_session: dict[str, list[dict]]
) -> list[Path]:
    """Write one .jsonl per non-empty session under target_dir/prefix.

    Returns the list of written file paths. The prefix dir is removed and
    recreated so re-runs never leave stale files; combined with deterministic
    uuids, the ingester re-ingests the identical content idempotently.
    """
    out_dir = Path(target_dir) / prefix
    if out_dir.exists():
        if not out_dir.is_dir():
            raise ValueError(f"out_dir exists but is not a directory: {out_dir}")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    seen_stems: dict[str, str] = {}  # stem -> originating session_name
    for session_name, rows in sorted(rows_by_session.items()):
        if not rows:
            continue
        stem = _safe_filename(session_name)
        if stem in seen_stems:
            raise ValueError(
                f"Filename collision: {session_name!r} and {seen_stems[stem]!r} "
                f"both sanitise to {stem!r}.jsonl"
            )
        seen_stems[stem] = session_name
        dst = out_dir / f"{stem}.jsonl"
        with dst.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False))
                fh.write("\n")
        written.append(dst)
    return written
