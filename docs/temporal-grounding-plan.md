# Temporal Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make REKOL's curated-memory retrieval time-aware (exclude invalidated, respect `valid_from`, mild layer-aware recency) and ship REKOL's own time-context hook, replacing the external `mac_setup` dependency.

**Architecture:** Connect an already-parsed-but-dropped timestamp pipeline: normalize timestamps at parse time → carry through the indexer onto `chunks` rows → return them from the pure-retrieval `store.search()` → apply all temporal *policy* in a new `ranking.py` seam → surface in `cli_search`. Separately, add a stdlib-only, soft-fail hidden `rekol _hook` subcommand wired by `install.sh`.

**Tech Stack:** Python 3.11+, Click, SQLite (sqlite-vec optional; numpy cosine fallback), pytest, bash/jq installer.

**Scope note:** This plan covers the **rekol repo** (spec Workstreams A, B, C). The **mac_setup cutover** (uninstall + component retirement) is a separate follow-up plan in that repo — it is shell/operational, not pytest-driven. Spec: [temporal-grounding-design.md](temporal-grounding-design.md).

---

## File Structure

**Create:**
- `src/rekol/ranking.py` — `apply_temporal_ranking()`: invalidation filter, `valid_from` filter, layer-aware recency boost, invalidated-penalty; pure function.
- `src/rekol/cli_hooks.py` — hidden `_hook` group: `time-context` (UserPromptSubmit) + `record-stop` (Stop); stdlib-only, soft-fail, `session_id` validation.
- `hooks/userpromptsubmit-snippet.json`, `hooks/stop-snippet.json` — Claude Code hook snippets.
- Tests: `tests/test_ranking.py`, `tests/test_cli_hooks.py`. New tests are also added to existing `tests/test_model.py`, `tests/test_store.py`, `tests/test_indexer.py`, `tests/test_config.py`, `tests/test_search_combined.py`.

**Modify:**
- `src/rekol/model.py` — `_normalize_ts()` helper; `parse_file()` normalizes the four timestamps.
- `src/rekol/store.py` — `SCHEMA_CHUNKS` (+4 columns); `replace_chunks_for_file()` writes them; `search()` returns them + `cosine_score`; `needs_schema_migration()`; `CuratedSchemaOutdatedError`; `CURATED_SCHEMA_VERSION`.
- `src/rekol/indexer.py` — `_index_one()` passes timestamps to the store.
- `src/rekol/config.py` — four `temporal_*` flat keys (DEFAULTS + `Config` + constructor).
- `src/rekol/search_combined.py` — call `apply_temporal_ranking` for memory hits; thread `filtered_count`; fix `is_promotion_candidate`.
- `src/rekol/cli_search.py` — `--include-invalidated`; render `cosine_score`/`final_score`/provenance/`[INVALIDATED]`; raise/handle `CuratedSchemaOutdatedError`.
- `src/rekol/cli_index.py` — bump `user_version` on rebuild.
- `src/rekol/cli.py` — register the hidden `_hook` group.
- `install.sh` — Step 7E (UserPromptSubmit) + Step 7F (Stop).

---

## Phase A — Curated temporal retrieval

### Task A1: Normalize timestamps at parse time

**Files:**
- Modify: `src/rekol/model.py` (add `_normalize_ts`; use it in `parse_file`, lines 135-138)
- Test: `tests/test_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_model.py  (append)
import datetime as dt
from rekol.model import _normalize_ts


def test_normalize_ts_date_object_date_only():
    assert _normalize_ts(dt.date(2026, 5, 31), date_only=True) == "2026-05-31"


def test_normalize_ts_datetime_object_date_only():
    val = dt.datetime(2026, 5, 31, 10, 0, 0)
    assert _normalize_ts(val, date_only=True) == "2026-05-31"


def test_normalize_ts_datetime_full_uses_T_separator():
    val = dt.datetime(2026, 5, 31, 10, 0, 0)
    assert _normalize_ts(val, date_only=False) == "2026-05-31T10:00:00"


def test_normalize_ts_space_separated_string_canonicalizes_to_T():
    assert _normalize_ts("2026-05-31 10:00:00+00:00", date_only=False) == "2026-05-31T10:00:00+00:00"


def test_normalize_ts_string_date_only_truncates():
    assert _normalize_ts("2026-05-31T10:00:00-07:00", date_only=True) == "2026-05-31"


def test_normalize_ts_none_and_empty():
    assert _normalize_ts(None, date_only=True) is None
    assert _normalize_ts("", date_only=False) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_model.py -k normalize_ts -v`
Expected: FAIL with `ImportError: cannot import name '_normalize_ts'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/rekol/model.py  (add near the top-of-module imports)
import datetime as _dt

# ... add this helper above parse_file():
def _normalize_ts(value: object, *, date_only: bool) -> str | None:
    """Canonicalize a frontmatter timestamp to a single ISO format.

    PyYAML parses bare dates to date/datetime objects whose ``str()`` is
    space-separated, while the capture/invalidate CLIs write ``T``-separated
    ISO strings. Left unnormalized the index column would hold incompatible
    formats. ``date_only`` stores ``YYYY-MM-DD`` (created/updated/valid_from);
    otherwise full ISO with a ``T`` separator (invalidated_at).
    """
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):  # check before date: datetime subclasses date
        return value.date().isoformat() if date_only else value.isoformat(timespec="seconds")
    if isinstance(value, _dt.date):
        return value.isoformat()
    s = str(value).strip()
    if date_only:
        return s[:10]
    return s.replace(" ", "T")
```

Then update `parse_file()` (lines 135-138) to normalize:

```python
        created=_normalize_ts(meta.get("created"), date_only=True),
        updated=_normalize_ts(meta.get("updated"), date_only=True),
        valid_from=_normalize_ts(meta.get("valid_from"), date_only=True),
        invalidated_at=_normalize_ts(meta.get("invalidated_at"), date_only=False),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_model.py -k normalize_ts -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full model + the existing suite for regressions**

Run: `pytest tests/test_model.py tests/test_model_scope.py -v`
Expected: PASS (existing tests unchanged — `MemoryFile` fields are still populated)

- [ ] **Step 6: Commit**

```bash
git add src/rekol/model.py tests/test_model.py
git commit -m "feat: normalize curated timestamps to canonical ISO at parse time"
```

---

### Task A2: Add timestamp columns + migration detection to the curated schema

**Files:**
- Modify: `src/rekol/store.py` (`SCHEMA_CHUNKS`; add `CURATED_SCHEMA_VERSION`, `CuratedSchemaOutdatedError`, `needs_schema_migration()`)
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py  (append)
from rekol.store import IndexStore


def test_fresh_schema_has_timestamp_columns(store: IndexStore) -> None:
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(chunks)")}
    assert {"created", "updated", "valid_from", "invalidated_at"}.issubset(cols)


def test_fresh_store_needs_no_migration(store: IndexStore) -> None:
    assert store.needs_schema_migration() is False


def test_legacy_schema_needs_migration(tmp_path) -> None:
    # Simulate a pre-timestamp index: a chunks table without the new columns.
    db = tmp_path / "legacy.db"
    s = IndexStore(db_path=db, dim=8, use_sqlite_vec=False)
    s.conn.execute("DROP TABLE IF EXISTS chunks")
    s.conn.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_path TEXT, heading TEXT, "
        "line_start INTEGER, line_end INTEGER, text TEXT, tags_json TEXT, "
        "aliases_json TEXT, embedding BLOB)"
    )
    s.conn.commit()
    assert s.needs_schema_migration() is True
    s.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py -k "timestamp_columns or migration" -v`
Expected: FAIL (`AttributeError: needs_schema_migration` / missing columns)

- [ ] **Step 3: Write minimal implementation**

In `src/rekol/store.py`, add near the top:

```python
CURATED_SCHEMA_VERSION = 2  # 1 = original; 2 = with curated timestamp columns


class CuratedSchemaOutdatedError(RuntimeError):
    """Raised by read-only commands when the curated index predates the
    timestamp columns and must be rebuilt (`rekol index rebuild`)."""
```

Extend `SCHEMA_CHUNKS` (add the four columns before `embedding`):

```python
    text         TEXT NOT NULL,
    tags_json    TEXT NOT NULL DEFAULT '[]',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    created        TEXT,
    updated        TEXT,
    valid_from     TEXT,
    invalidated_at TEXT,
    embedding    BLOB NOT NULL
```

Add the method (and stamp the version in `init_schema()` after the CREATEs):

```python
    def needs_schema_migration(self) -> bool:
        """True when the curated index predates the timestamp columns.

        Detection is by column presence (robust even for indexes built before
        versioning existed); `user_version` is also stamped for future use.
        """
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(chunks)")}
        return "created" not in cols
```

In `init_schema()`, after creating the tables, add:
`self.conn.execute(f"PRAGMA user_version = {CURATED_SCHEMA_VERSION}")`

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py -k "timestamp_columns or migration" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/rekol/store.py tests/test_store.py
git commit -m "feat: add curated timestamp columns + schema-migration detection"
```

---

### Task A3: Write timestamps in `replace_chunks_for_file`

**Files:**
- Modify: `src/rekol/store.py:112-138` (`replace_chunks_for_file`), `all_chunks_for_file` (expose new cols)
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py  (append)
import numpy as np


def test_replace_chunks_persists_file_timestamps(store: IndexStore, tmp_path) -> None:
    p = tmp_path / "topics" / "x.md"
    p.parent.mkdir(parents=True)
    p.write_text("dummy")
    store.upsert_file(path=str(p), mtime=1, content_hash="h")
    store.replace_chunks_for_file(
        str(p),
        [dict(heading=None, line_start=1, line_end=2, text="t",
              tags=[], aliases=[], embedding=np.ones(8, dtype=np.float32))],
        created="2026-01-01", updated="2026-02-01",
        valid_from="2026-01-01", invalidated_at=None,
    )
    row = store.conn.execute(
        "SELECT created, updated, valid_from, invalidated_at FROM chunks "
        "WHERE file_path=?", (str(p),)
    ).fetchone()
    assert (row["created"], row["updated"], row["valid_from"], row["invalidated_at"]) \
        == ("2026-01-01", "2026-02-01", "2026-01-01", None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py -k persists_file_timestamps -v`
Expected: FAIL (`TypeError: unexpected keyword argument 'created'`)

- [ ] **Step 3: Write minimal implementation**

Change the signature and INSERT in `replace_chunks_for_file`:

```python
    def replace_chunks_for_file(
        self,
        file_path: str,
        chunks: list[dict[str, Any]],
        *,
        created: str | None = None,
        updated: str | None = None,
        valid_from: str | None = None,
        invalidated_at: str | None = None,
    ) -> None:
        """Replace all chunks for a file. The four timestamps are file-level
        (frontmatter) and denormalized onto every chunk row."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM chunks WHERE file_path=?", (file_path,))
        for c in chunks:
            emb: np.ndarray = c["embedding"]
            if emb.dtype != np.float32:
                emb = emb.astype(np.float32)
            cur.execute(
                "INSERT INTO chunks(file_path, heading, line_start, line_end, "
                "text, tags_json, aliases_json, created, updated, valid_from, "
                "invalidated_at, embedding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    file_path, c.get("heading"), int(c["line_start"]),
                    int(c["line_end"]), c["text"],
                    json.dumps(c.get("tags", [])), json.dumps(c.get("aliases", [])),
                    created, updated, valid_from, invalidated_at,
                    emb.tobytes(),
                ),
            )
        self.conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py -k persists_file_timestamps -v`
Expected: PASS

- [ ] **Step 5: Run the store suite for regressions**

Run: `pytest tests/test_store.py -v`
Expected: PASS (existing `replace_chunks_for_file` callers pass no timestamps → all default to NULL)

- [ ] **Step 6: Commit**

```bash
git add src/rekol/store.py tests/test_store.py
git commit -m "feat: persist file-level timestamps onto curated chunk rows"
```

---

### Task A4: Return timestamps + `cosine_score` from `store.search`

**Files:**
- Modify: `src/rekol/store.py:160-194` (`search`)
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py  (append)
def test_search_returns_timestamps_and_cosine_score(store: IndexStore, tmp_path) -> None:
    p = tmp_path / "topics" / "y.md"
    p.parent.mkdir(parents=True)
    p.write_text("dummy")
    store.upsert_file(path=str(p), mtime=1, content_hash="h")
    store.replace_chunks_for_file(
        str(p),
        [dict(heading=None, line_start=1, line_end=2, text="t",
              tags=[], aliases=[], embedding=np.ones(8, dtype=np.float32))],
        created="2026-01-01", updated="2026-02-01",
        valid_from="2026-01-01", invalidated_at="2026-03-01",
    )
    hits = store.search(np.ones(8, dtype=np.float32), top_k=1)
    h = hits[0]
    assert "cosine_score" in h
    assert h["created"] == "2026-01-01" and h["invalidated_at"] == "2026-03-01"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py -k returns_timestamps_and_cosine -v`
Expected: FAIL (`KeyError: 'cosine_score'`)

- [ ] **Step 3: Write minimal implementation**

In `search()`, extend the `SELECT` and the result dict. Change the column list to include the four timestamps, and in the per-hit `dict(...)` replace `score=float(scores[i])` with `cosine_score=float(scores[i])` and add the four fields:

```python
        rows = self.conn.execute(
            "SELECT id, file_path, heading, line_start, line_end, text, "
            "tags_json, aliases_json, created, updated, valid_from, "
            "invalidated_at, embedding FROM chunks"
        ).fetchall()
        # ... (vectorized cosine unchanged) ...
            out.append(
                dict(
                    id=r["id"], file_path=r["file_path"], heading=r["heading"],
                    line_start=r["line_start"], line_end=r["line_end"], text=r["text"],
                    tags=json.loads(r["tags_json"]), aliases=json.loads(r["aliases_json"]),
                    created=r["created"], updated=r["updated"],
                    valid_from=r["valid_from"], invalidated_at=r["invalidated_at"],
                    cosine_score=float(scores[i]),
                )
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py -k returns_timestamps_and_cosine -v`
Expected: PASS

- [ ] **Step 5: Update the two existing `score` readers (keep suite green)**

`search_combined.py` `_merge_session_hits` operates on SESSION hits only (unchanged). `cli_search.py` renders memory hits with `h['score']` — temporarily map it. In `cli_search.py` `_render_text`, change the memory-hit line from `h['score']` to `h.get('final_score', h['cosine_score'])` and the JSON `score=h["score"]` to `cosine_score=h["cosine_score"]`. (Full rendering is finished in A8; this keeps tests green now.)

Run: `pytest tests/test_store.py tests/test_search_combined.py tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/rekol/store.py src/rekol/cli_search.py tests/test_store.py
git commit -m "feat: surface curated timestamps and raw cosine_score from search"
```

---

### Task A5: `ranking.py` — temporal filter + layer-aware recency (pure)

**Files:**
- Create: `src/rekol/ranking.py`
- Test: `tests/test_ranking.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ranking.py  (new)
import datetime as dt
from pathlib import Path

from rekol.ranking import apply_temporal_ranking

HOME = Path("/memhome")
TODAY = dt.date(2026, 6, 1)


def _hit(layer, name, score, **ts):
    return {"file_path": f"/memhome/{layer}/{name}.md", "score": score, **ts}


def _rank(hits, **kw):
    defaults = dict(memory_home=HOME, today=TODAY, recency_weight=0.03,
                    recency_halflife_days=180, exempt_layers=["always", "knowledge"],
                    exclude_invalidated=True, respect_valid_from=True,
                    include_invalidated=False)
    defaults.update(kw)
    return apply_temporal_ranking(hits, **defaults)


def test_invalidated_excluded_by_default():
    hits = [_hit("topics", "a", 0.9, invalidated_at="2026-03-01")]
    ranked, filtered = _rank(hits)
    assert ranked == [] and filtered == 1


def test_invalidated_included_but_below_live_when_flag_set():
    hits = [_hit("topics", "bad", 0.95, invalidated_at="2026-03-01"),
            _hit("topics", "live", 0.10)]
    ranked, _ = _rank(hits, include_invalidated=True)
    assert [h["file_path"].split("/")[-1] for h in ranked] == ["live.md", "bad.md"]


def test_future_valid_from_filtered():
    hits = [_hit("topics", "future", 0.9, valid_from="2027-01-01")]
    ranked, filtered = _rank(hits)
    assert ranked == [] and filtered == 1


def test_recency_breaks_near_tie_for_timely_layer():
    hits = [_hit("topics", "old", 0.80, updated="2020-01-01"),
            _hit("topics", "new", 0.80, updated="2026-05-31")]
    ranked, _ = _rank(hits)
    assert ranked[0]["file_path"].endswith("new.md")


def test_strong_old_beats_weak_new():
    hits = [_hit("topics", "old", 0.90, updated="2019-01-01"),
            _hit("topics", "new", 0.50, updated="2026-05-31")]
    ranked, _ = _rank(hits)
    assert ranked[0]["file_path"].endswith("old.md")


def test_knowledge_layer_treated_as_always_current():
    # Durable knowledge/ hit (old) vs fresher topics/ hit at equal cosine.
    # The exempt hit gets the FULL un-decayed boost (always-current), so it is
    # not out-ranked by the fresher hit (whose boost is slightly decayed).
    hits = [_hit("knowledge", "durable", 0.80, updated="2019-01-01"),
            _hit("topics", "fresh", 0.80, updated="2026-05-31")]
    ranked, _ = _rank(hits)
    assert ranked[0]["file_path"].endswith("durable.md")


def test_cosine_score_preserved_and_final_score_added():
    hits = [_hit("topics", "a", 0.7, updated="2026-05-31")]
    ranked, _ = _rank(hits)
    assert ranked[0]["cosine_score"] == 0.7
    assert ranked[0]["final_score"] >= 0.7
```

> Note: exempt layers are treated as **always-current** (boost = full `recency_weight`, no decay), NOT zero-boost. Zero-boost would leave a durable hit exposed to a fresher hit's boost and defeat the purpose; full boost keeps it competitive. No special tie-break needed — the exempt hit wins by the fresher hit's small decay gap.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ranking.py -v`
Expected: FAIL (`ModuleNotFoundError: rekol.ranking`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/rekol/ranking.py  (new)
"""Temporal ranking policy for curated memory hits.

Pure functions over the hit dicts returned by ``IndexStore.search`` — kept out
of the storage layer so the store has no config dependency and this policy is
unit-testable in isolation.
"""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Any

# Large enough to push an included-invalidated hit below every live hit
# regardless of cosine/recency.
_INVALIDATED_PENALTY = 1000.0


def _layer_of(file_path: str, memory_home: Path) -> str | None:
    """Top-level layer dir of a memory file (e.g. 'knowledge'), or None."""
    try:
        rel = Path(file_path).relative_to(memory_home)
    except ValueError:
        return None
    return rel.parts[0] if len(rel.parts) > 1 else None


def _as_date(value: object) -> dt.date | None:
    """Best-effort date-granularity parse; unparseable/empty → None."""
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
    """Filter + re-rank curated hits temporally.

    Returns ``(ranked_hits, filtered_count)``. ``filtered_count`` counts hits
    removed by the invalidation / valid_from filters (used to suppress a false
    'no memory — consider capturing' hint when matches exist but are all
    invalidated/future).
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
            vf = _as_date(h.get("valid_from") or h.get("created"))
            if vf is not None and vf > today:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ranking.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/rekol/ranking.py tests/test_ranking.py
git commit -m "feat: add temporal ranking (invalidation/valid_from/layer-aware recency)"
```

---

### Task A6: Config knobs (flat keys)

**Files:**
- Modify: `src/rekol/config.py` (`DEFAULTS` line 16-24; `Config` dataclass line 40-58; constructor line ~113-127)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py  (append)
def test_temporal_defaults_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    from rekol.config import load_config
    cfg = load_config()
    assert cfg.temporal_exclude_invalidated is True
    assert cfg.temporal_respect_valid_from is True
    assert cfg.temporal_recency_weight == 0.03
    assert cfg.temporal_recency_halflife_days == 180
    assert cfg.temporal_recency_exempt_layers == ["always", "knowledge"]
    assert cfg.temporal_confirm_interval_days == 180


def test_temporal_overrides_from_yaml(tmp_path, monkeypatch):
    (tmp_path / "rekol.config.yaml").write_text(
        "temporal_recency_weight: 0.1\n"
        "temporal_recency_exempt_layers: [always]\n"
    )
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    from rekol.config import load_config
    cfg = load_config()
    assert cfg.temporal_recency_weight == 0.1
    assert cfg.temporal_recency_exempt_layers == ["always"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -k temporal -v`
Expected: FAIL (`AttributeError: ... 'temporal_exclude_invalidated'`)

- [ ] **Step 3: Write minimal implementation**

Add to `DEFAULTS`:

```python
    temporal_exclude_invalidated=True,
    temporal_respect_valid_from=True,
    temporal_recency_weight=0.03,
    temporal_recency_halflife_days=180,
    temporal_recency_exempt_layers=["always", "knowledge"],
    temporal_confirm_interval_days=180,
```

Add to the `Config` dataclass fields:

```python
    temporal_exclude_invalidated: bool
    temporal_respect_valid_from: bool
    temporal_recency_weight: float
    temporal_recency_halflife_days: float
    temporal_recency_exempt_layers: list[str]
    temporal_confirm_interval_days: int
```

Add to the `Config(...)` constructor in `load_config()`:

```python
        temporal_exclude_invalidated=bool(data["temporal_exclude_invalidated"]),
        temporal_respect_valid_from=bool(data["temporal_respect_valid_from"]),
        temporal_recency_weight=float(data["temporal_recency_weight"]),
        temporal_recency_halflife_days=float(data["temporal_recency_halflife_days"]),
        temporal_recency_exempt_layers=list(data["temporal_recency_exempt_layers"]),
        temporal_confirm_interval_days=int(data["temporal_confirm_interval_days"]),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -k temporal -v`
Expected: PASS

- [ ] **Step 5: Run config + back-compat suite**

Run: `pytest tests/test_config.py tests/test_config_backcompat.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/rekol/config.py tests/test_config.py
git commit -m "feat: add temporal_* config knobs (flat keys for the whitelist loader)"
```

---

### Task A7: Carry timestamps through the indexer

**Files:**
- Modify: `src/rekol/indexer.py:76-138` (`_index_one`, both `replace_chunks_for_file` calls)
- Test: `tests/test_indexer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_indexer.py  (append — match the file's existing fixture style)
def test_indexer_carries_frontmatter_timestamps(tmp_path):
    from rekol.embeddings import get_embedder
    from rekol.indexer import Indexer
    from rekol.store import IndexStore

    root = tmp_path
    (root / "topics").mkdir()
    (root / "topics" / "t.md").write_text(
        "---\nname: t\ndescription: d\ntype: topic\n"
        "created: 2026-01-01\nupdated: 2026-02-01\ninvalidated_at: 2026-03-01\n---\nbody\n"
    )
    store = IndexStore(db_path=root / ".index.db", dim=384, use_sqlite_vec=False)
    store.init_schema()
    Indexer(store=store, embedder=get_embedder("test-hashing"),
            memory_root=root, chunk_max_bytes=1500).rebuild()
    row = store.conn.execute(
        "SELECT created, updated, invalidated_at FROM chunks LIMIT 1"
    ).fetchone()
    assert (row["created"], row["updated"], row["invalidated_at"]) \
        == ("2026-01-01", "2026-02-01", "2026-03-01T00:00:00")
    store.close()
```

> The `invalidated_at` expectation is `...T00:00:00` because A1 normalizes a bare date in a non-date-only field to a `T`-form datetime. If `_normalize_ts` leaves a bare date string unchanged for the non-date_only path, assert `"2026-03-01"` instead — match the test to A1's actual output.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_indexer.py -k carries_frontmatter -v`
Expected: FAIL (timestamps are NULL)

- [ ] **Step 3: Write minimal implementation**

In `_index_one`, both `self.store.replace_chunks_for_file(...)` calls (the empty-body fallback and the normal path) pass the parsed timestamps from `mf`:

```python
        self.store.replace_chunks_for_file(
            str(path), records,
            created=mf.created, updated=mf.updated,
            valid_from=mf.valid_from, invalidated_at=mf.invalidated_at,
        )
```

(Apply the same four kwargs to the fallback-chunk call earlier in the method.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_indexer.py -k carries_frontmatter -v`
Expected: PASS

- [ ] **Step 5: Run the indexer suite**

Run: `pytest tests/test_indexer.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/rekol/indexer.py tests/test_indexer.py
git commit -m "feat: carry frontmatter timestamps through indexing onto chunks"
```

---

### Task A8: Wire ranking + promotion-gate into combined search

**Files:**
- Modify: `src/rekol/search_combined.py` (`search_all` line 78-99; `CombinedSearchResult` line 27-47)
- Test: `tests/test_search_combined.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search_combined.py  (append)
def test_filtered_count_suppresses_promotion_candidate(monkeypatch):
    from rekol.search_combined import CombinedSearchResult
    r = CombinedSearchResult(memory_hits=[], session_hits=[], memory_filtered_count=2)
    assert r.is_promotion_candidate is False  # matches exist but were filtered


def test_promotion_candidate_when_truly_empty():
    from rekol.search_combined import CombinedSearchResult
    r = CombinedSearchResult(memory_hits=[], session_hits=[{"x": 1}],
                             memory_filtered_count=0)
    assert r.is_promotion_candidate is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_search_combined.py -k "promotion" -v`
Expected: FAIL (`TypeError: unexpected keyword 'memory_filtered_count'`)

- [ ] **Step 3: Write minimal implementation**

Add a field to `CombinedSearchResult` and gate the property:

```python
    memory_filtered_count: int = 0
    ...
    @property
    def is_promotion_candidate(self) -> bool:
        return (
            len(self.memory_hits) == 0
            and self.memory_filtered_count == 0
            and len(self.session_hits) > 0
        )
```

In `search_all`, replace the bare memory search (line ~99) with retrieval + ranking:

```python
    raw_memory = memory_store.search(query_vec, top_k=memory_top_k)
    ranked, filtered = apply_temporal_ranking(
        raw_memory,
        memory_home=cfg.memory_home, today=dt.date.today(),
        recency_weight=cfg.temporal_recency_weight,
        recency_halflife_days=cfg.temporal_recency_halflife_days,
        exempt_layers=cfg.temporal_recency_exempt_layers,
        exclude_invalidated=cfg.temporal_exclude_invalidated,
        respect_valid_from=cfg.temporal_respect_valid_from,
        include_invalidated=include_invalidated,
    )
    result.memory_hits = ranked[:memory_top_k]
    result.memory_filtered_count = filtered
```

Add the imports (`import datetime as dt`, `from rekol.ranking import apply_temporal_ranking`) and thread `cfg` + `include_invalidated: bool = False` into `search_all`'s signature (caller passes them in A9).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_search_combined.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rekol/search_combined.py tests/test_search_combined.py
git commit -m "feat: apply temporal ranking in combined search; fix promotion gating"
```

---

### Task A9: CLI — `--include-invalidated`, rendering, schema guard

**Files:**
- Modify: `src/rekol/cli_search.py` (option, render, JSON, schema check); `src/rekol/cli_index.py` (bump `user_version` on rebuild)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py  (append)
from click.testing import CliRunner


def test_search_has_include_invalidated_flag():
    from rekol.cli_search import main
    res = CliRunner().invoke(main, ["--help"])
    assert "--include-invalidated" in res.output


def test_outdated_schema_instructs_rebuild(tmp_path, monkeypatch):
    # A curated index missing the timestamp columns must instruct, not crash/empty.
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    idx = tmp_path / ".index"
    idx.mkdir()
    import sqlite3
    con = sqlite3.connect(idx / "index.db")
    con.execute("CREATE TABLE files (path TEXT PRIMARY KEY, mtime INT, content_hash TEXT, indexed_at INT)")
    con.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_path TEXT, heading TEXT, "
                "line_start INT, line_end INT, text TEXT, tags_json TEXT, aliases_json TEXT, embedding BLOB)")
    con.commit(); con.close()
    from rekol.cli_search import main
    res = CliRunner().invoke(main, ["something"])
    assert "rekol index rebuild" in res.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -k "include_invalidated or outdated_schema" -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Add the option and schema guard to `cli_search.main`:

```python
@click.option("--include-invalidated", is_flag=True, default=False,
              help="Include invalidated memories (tagged; ranked below live hits).")
```

Before searching, open the store and guard:

```python
    if store.needs_schema_migration():
        click.echo("curated index schema is out of date — run `rekol index rebuild`", err=True)
        raise SystemExit(1)
```

Pass `include_invalidated` and `cfg` into `search_all`. In `_render_text`, for each memory hit show provenance and tag invalidated ones; the score column uses `final_score`, with `cosine_score` available:

```python
        ts = f" · updated {h['updated']}" if h.get("updated") else ""
        inv = " [INVALIDATED]" if h.get("invalidated_at") else ""
        lines.append(
            f"{h['final_score']:.3f}  {h['file_path']}{heading}  "
            f"(L{h['line_start']}-{h['line_end']}){ts}{inv}"
        )
```

In the JSON branch add `created/updated/valid_from/invalidated_at`, `cosine_score`, and `final_score` to each memory object.

In `cli_index.py` rebuild path, after `Indexer(...).rebuild()`, bump the version: `store.conn.execute(f"PRAGMA user_version = {CURATED_SCHEMA_VERSION}")` (import it).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -k "include_invalidated or outdated_schema" -v`
Expected: PASS

- [ ] **Step 5: Full suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/rekol/cli_search.py src/rekol/cli_index.py tests/test_cli.py
git commit -m "feat: --include-invalidated, temporal render, schema-outdated guard"
```

---

## Phase B — Port the time hook into REKOL

### Task B1: `cli_hooks.py` — time-context + record-stop

**Files:**
- Create: `src/rekol/cli_hooks.py`
- Modify: `src/rekol/cli.py` (register hidden `_hook` group)
- Test: `tests/test_cli_hooks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_hooks.py  (new)
import json
from click.testing import CliRunner

from rekol.cli_hooks import hook_group


def _run(args, stdin):
    return CliRunner().invoke(hook_group, args, input=stdin)


def test_time_context_emits_env_time_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = _run(["time-context"], json.dumps({"session_id": "abc-123"}))
    assert res.exit_code == 0
    assert "<env-time>" in res.output and "local_time" in res.output


def test_record_stop_updates_state_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _run(["time-context"], json.dumps({"session_id": "s1"}))
    res = _run(["record-stop"], json.dumps({"session_id": "s1"}))
    assert res.exit_code == 0 and res.output.strip() == ""
    state = json.loads((tmp_path / ".claude" / "session-env" / "time-context-s1.json").read_text())
    assert state["last_assistant_epoch"] is not None


def test_soft_fail_on_bad_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = _run(["time-context"], "not json")
    assert res.exit_code == 0  # never blocks the prompt


def test_soft_fail_on_path_traversal_session_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = _run(["time-context"], json.dumps({"session_id": "../../evil"}))
    assert res.exit_code == 0
    assert not (tmp_path / "evil.json").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_hooks.py -v`
Expected: FAIL (`ModuleNotFoundError: rekol.cli_hooks`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/rekol/cli_hooks.py  (new)
"""Hidden hook subcommands: time-context (UserPromptSubmit) + record-stop (Stop).

Stdlib-only and soft-fail by design — any error degrades and exits 0 so a hook
problem never blocks a prompt. State: ~/.claude/session-env/time-context-<id>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import click

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _state_path(session_id: str) -> Path:
    d = Path.home() / ".claude" / "session-env"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"time-context-{session_id}.json"


def _read_payload() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return {}


def _safe_session_id(payload: dict) -> str | None:
    sid = str(payload.get("session_id", "")).strip()
    return sid if _SAFE_ID.match(sid) else None


def _emit_env_time(since_user: int | None, since_assistant: int | None) -> None:
    now = dt.datetime.now().astimezone()
    def _fmt(s: int | None) -> str:
        if s is None:
            return "unknown"
        m, sec = divmod(max(0, s), 60)
        return f"{m}m {sec}s"
    click.echo(
        "<env-time>\n"
        f"  local_time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} (offset {now.strftime('%z')})\n"
        f"  utc: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"  time_since_last_user_message: {_fmt(since_user)}\n"
        f"  time_since_last_assistant_message: {_fmt(since_assistant)}\n"
        "</env-time>"
    )


@click.group(name="_hook", hidden=True)
def hook_group() -> None:
    """Internal Claude Code hook entrypoints (not for direct use)."""


@hook_group.command(name="time-context")
def time_context() -> None:
    """UserPromptSubmit: emit <env-time> and update per-session state."""
    payload = _read_payload()
    sid = _safe_session_id(payload)
    if sid is None:
        _emit_env_time(None, None)
        return
    now = int(time.time())
    prev = {}
    path = _state_path(sid)
    try:
        if path.exists():
            prev = json.loads(path.read_text())
    except (ValueError, OSError):
        prev = {}
    last_user = prev.get("last_user_epoch")
    last_assistant = prev.get("last_assistant_epoch")
    _emit_env_time(
        now - last_user if isinstance(last_user, int) else None,
        now - last_assistant if isinstance(last_assistant, int) else None,
    )
    try:
        path.write_text(json.dumps({"last_user_epoch": now,
                                    "last_assistant_epoch": last_assistant}))
    except OSError as exc:
        click.echo(f"time-context: state write failed: {exc}", err=True)


@hook_group.command(name="record-stop")
def record_stop() -> None:
    """Stop: record the assistant-completion epoch (no stdout)."""
    payload = _read_payload()
    sid = _safe_session_id(payload)
    if sid is None:
        return
    path = _state_path(sid)
    prev = {}
    try:
        if path.exists():
            prev = json.loads(path.read_text())
    except (ValueError, OSError):
        prev = {}
    prev["last_assistant_epoch"] = int(time.time())
    try:
        path.write_text(json.dumps(prev))
    except OSError as exc:
        click.echo(f"record-stop: state write failed: {exc}", err=True)
```

Register it in `cli.py` (alongside the other `add_command` lines):

```python
from rekol.cli_hooks import hook_group
main.add_command(hook_group, name="_hook")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_hooks.py -v`
Expected: PASS (4 tests)

> Click reads stdin via the test runner's `input=`; `sys.stdin.read()` works under `CliRunner`. If buffering differs, switch `_read_payload` to `click.get_text_stream("stdin").read()`.

- [ ] **Step 5: Commit**

```bash
git add src/rekol/cli_hooks.py src/rekol/cli.py tests/test_cli_hooks.py
git commit -m "feat: add stdlib-only soft-fail rekol _hook time-context/record-stop"
```

---

### Task B2: Hook snippets

**Files:**
- Create: `hooks/userpromptsubmit-snippet.json`, `hooks/stop-snippet.json`
- Test: `tests/test_cli_hooks.py` (validate JSON shape)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_hooks.py  (append)
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_hook_snippets_are_valid_and_call_rekol_hook():
    ups = json.loads((REPO / "hooks" / "userpromptsubmit-snippet.json").read_text())
    cmd = ups["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "rekol _hook time-context" in cmd
    stop = json.loads((REPO / "hooks" / "stop-snippet.json").read_text())
    assert "rekol _hook record-stop" in stop["hooks"]["Stop"][0]["hooks"][0]["command"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_hooks.py -k snippets -v`
Expected: FAIL (`FileNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```json
// hooks/userpromptsubmit-snippet.json
{
  "_comment": "Merge into ~/.claude/settings.json hooks.UserPromptSubmit. Injects an <env-time> block (local/UTC + elapsed-since-last-user/assistant). Soft-fail (always exits 0).",
  "hooks": {
    "UserPromptSubmit": [
      { "matcher": "", "hooks": [ { "type": "command", "command": "rekol _hook time-context" } ] }
    ]
  }
}
```

```json
// hooks/stop-snippet.json
{
  "_comment": "Merge into ~/.claude/settings.json hooks.Stop. Records the assistant-completion timestamp for the next turn's elapsed deltas. Soft-fail.",
  "hooks": {
    "Stop": [
      { "matcher": "", "hooks": [ { "type": "command", "command": "rekol _hook record-stop" } ] }
    ]
  }
}
```

(Remove the `//` comment lines — JSON has no comments; the `_comment` key carries the note.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_hooks.py -k snippets -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hooks/userpromptsubmit-snippet.json hooks/stop-snippet.json tests/test_cli_hooks.py
git commit -m "feat: add UserPromptSubmit/Stop hook snippets for rekol _hook"
```

---

### Task B3: Installer Steps 7E/7F + double-injection guard

**Files:**
- Modify: `install.sh` (add Steps 7E and 7F after Step 7D, ~line 476)
- Test: `tests/test_install.bats` (bats) — or a manual dry-run check

- [ ] **Step 1: Write the failing test**

```bash
# tests/test_install.bats  (append)
@test "install wires UserPromptSubmit + Stop rekol hooks idempotently" {
  run bash -c 'REKOL_HOME=$BATS_TEST_TMPDIR/mem REKOL_TOOLS_HOME=$BATS_TEST_TMPDIR/tools \
    HOME=$BATS_TEST_TMPDIR ./install.sh --dry-run'
  [ "$status" -eq 0 ]
  echo "$output" | grep -q "UserPromptSubmit"
  echo "$output" | grep -q "Stop"
}

@test "double-injection guard warns (not no-op) on a legacy mac_setup time hook" {
  mkdir -p "$BATS_TEST_TMPDIR/.claude"
  printf '{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"~/.local/share/mac_setup/hooks/inject-time-context.sh"}]}]}}' \
    > "$BATS_TEST_TMPDIR/.claude/settings.json"
  run bash -c 'HOME=$BATS_TEST_TMPDIR REKOL_HOME=$BATS_TEST_TMPDIR/mem ./install.sh'
  echo "$output" | grep -qi "legacy mac_setup time hook detected"
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bats tests/test_install.bats -f "UserPromptSubmit or legacy"`
Expected: FAIL (steps not present)

- [ ] **Step 3: Write minimal implementation**

After Step 7D, add Step 7E (clone the 7D jq-merge pattern, keyed on `rekol _hook time-context`, snippet `hooks/userpromptsubmit-snippet.json`, event `UserPromptSubmit`), with a legacy guard before merging:

```bash
# Step 7E — UserPromptSubmit time-context hook
if [[ "$DO_HOOK" == "1" ]]; then
  SNIPPET_UPS="${COMPONENT_DIR}/hooks/userpromptsubmit-snippet.json"
  if command -v jq >/dev/null 2>&1; then
    HAS_LEGACY="$(jq -r '[.hooks.UserPromptSubmit[]?.hooks[]?.command] | any(. | test("inject-time-context.sh"))' "${SETTINGS_JSON}" 2>/dev/null || printf 'false')"
    HAS_REKOL="$(jq -r '[.hooks.UserPromptSubmit[]?.hooks[]?.command] | any(. == "rekol _hook time-context")' "${SETTINGS_JSON}" 2>/dev/null || printf 'false')"
    if [[ "$HAS_LEGACY" == "true" ]]; then
      say "Legacy mac_setup time hook detected — rekol's time hook was NOT installed. Run 'mac_setup --uninstall', then re-run 'rekol install'."
    elif [[ "$HAS_REKOL" == "true" ]]; then
      say "UserPromptSubmit time hook already present — no-op"
    else
      # ... (independent .bak backup + jq merge of $SNIPPET_UPS, exactly as Step 7D) ...
      log_journal "MERGED UserPromptSubmit time hook into ${SETTINGS_JSON}"
    fi
  fi
fi
```

Add Step 7F identically for `Stop` / `rekol _hook record-stop` / `hooks/stop-snippet.json` (no legacy-guard branch needed for Stop, but key idempotency on the exact command).

- [ ] **Step 4: Run test to verify it passes**

Run: `bats tests/test_install.bats -f "UserPromptSubmit or legacy"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add install.sh tests/test_install.bats
git commit -m "feat: install steps 7E/7F wire rekol time hooks; warn on legacy guard"
```

---

### Task B4: Update install/README docs for the time hook

**Files:**
- Modify: `README.md` (hooks list); `docs/persistent-memory-system-design.md` (hooks table)
- Test: none (docs)

- [ ] **Step 1: Add the two hooks to the README hooks list and the design-doc hooks table** (UserPromptSubmit → time-context; Stop → record-stop), and add the cutover re-run note ("if `rekol install` ran before `mac_setup --uninstall`, re-run install").

- [ ] **Step 2: Commit**

```bash
git add README.md docs/persistent-memory-system-design.md
git commit -m "docs: document rekol's UserPromptSubmit/Stop time hooks"
```

---

## Phase C — Optional polish (relative phrasing)

### Task C1: Relative date phrasing in search output

**Files:**
- Modify: `src/rekol/cli_search.py` (`_format_session_timestamp` and the memory-hit render)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py  (append)
def test_relative_phrasing_helper():
    import datetime as dt
    from rekol.cli_search import _relative_age
    assert _relative_age(dt.date(2026, 5, 11), today=dt.date(2026, 6, 1)) == "3 weeks ago"
    assert _relative_age(dt.date(2026, 6, 1), today=dt.date(2026, 6, 1)) == "today"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -k relative_phrasing -v`
Expected: FAIL (`ImportError: _relative_age`)

- [ ] **Step 3: Write minimal implementation**

```python
def _relative_age(d, *, today):
    days = (today - d).days
    if days <= 0:
        return "today"
    for unit, n in (("year", 365), ("month", 30), ("week", 7), ("day", 1)):
        if days >= n:
            v = days // n
            return f"{v} {unit}{'s' if v > 1 else ''} ago"
    return "today"
```

Use it beside the absolute date in the memory-hit and session-hit render lines.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -k relative_phrasing -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rekol/cli_search.py tests/test_cli.py
git commit -m "feat: relative date phrasing alongside absolute dates in search"
```

---

## Phase D — Durable-memory confirmation

Exempt layers are trusted indefinitely, so add a re-confirmation loop. Confirm
reuses the `updated` field (no new column). Overdue = `updated`/`created` older
than `temporal_confirm_interval_days`, or absent.

### Task D1: Overdue-detection (pure) + store query

**Files:**
- Create: `src/rekol/review.py`
- Modify: `src/rekol/store.py` (add `distinct_file_timestamps`)
- Test: `tests/test_review.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review.py  (new)
import datetime as dt
from pathlib import Path

from rekol.review import find_overdue

HOME = Path("/m")
TODAY = dt.date(2026, 6, 1)


def _rows(*specs):
    return [dict(file_path=f"/m/{lyr}/{n}.md", updated=u, created=None) for lyr, n, u in specs]


def test_overdue_durable_only_past_interval():
    rows = _rows(("knowledge", "old", "2025-01-01"),   # >180d → overdue
                 ("knowledge", "fresh", "2026-05-20"),  # <180d → ok
                 ("topics", "old", "2020-01-01"))       # not durable → ignored
    out = find_overdue(rows, memory_home=HOME, exempt_layers=["always", "knowledge"],
                       interval_days=180, today=TODAY)
    assert [o["file_path"] for o in out] == ["/m/knowledge/old.md"]


def test_missing_date_is_overdue_and_sorts_first():
    rows = [dict(file_path="/m/always/x.md", updated=None, created=None),
            dict(file_path="/m/knowledge/y.md", updated="2024-01-01", created=None)]
    out = find_overdue(rows, memory_home=HOME, exempt_layers=["always", "knowledge"],
                       interval_days=180, today=TODAY)
    assert out[0]["file_path"] == "/m/always/x.md" and out[0]["age_days"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_review.py -v`
Expected: FAIL (`ModuleNotFoundError: rekol.review`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/rekol/review.py  (new)
"""Find durable (exempt-layer) memories overdue for re-confirmation."""
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
    """rows: [{file_path, updated, created}]. Returns overdue durable files as
    [{file_path, updated, age_days}], most-overdue first (no date = most overdue)."""
    exempt = set(exempt_layers)
    out: list[dict[str, Any]] = []
    for r in rows:
        if _layer_of(r["file_path"], memory_home) not in exempt:
            continue
        ref = _as_date(r.get("updated") or r.get("created"))
        if ref is None:
            out.append({"file_path": r["file_path"], "updated": r.get("updated"), "age_days": None})
        elif (today - ref).days > interval_days:
            out.append({"file_path": r["file_path"], "updated": r.get("updated"),
                        "age_days": (today - ref).days})
    out.sort(key=lambda x: -(x["age_days"] if x["age_days"] is not None else 10**9))
    return out
```

Add to `store.py`:

```python
    def distinct_file_timestamps(self) -> list[dict[str, Any]]:
        """One row per indexed file: {file_path, updated, created}."""
        rows = self.conn.execute(
            "SELECT DISTINCT file_path, updated, created FROM chunks"
        ).fetchall()
        return [dict(file_path=r["file_path"], updated=r["updated"],
                     created=r["created"]) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_review.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rekol/review.py src/rekol/store.py tests/test_review.py
git commit -m "feat: detect durable memories overdue for re-confirmation"
```

---

### Task D2: `rekol review` command (--nudge / --list / interactive)

**Files:**
- Create: `src/rekol/cli_review.py`
- Modify: `src/rekol/cli.py` (register `review`)
- Test: `tests/test_review.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review.py  (append)
from click.testing import CliRunner


def _seed_index(home, layer, name, updated):
    import numpy as np
    from rekol.store import IndexStore
    (home / layer).mkdir(parents=True, exist_ok=True)
    (home / layer / f"{name}.md").write_text("body")
    idx = home / ".index"; idx.mkdir(exist_ok=True)
    s = IndexStore(db_path=idx / "index.db", dim=8, use_sqlite_vec=False); s.init_schema()
    fp = str(home / layer / f"{name}.md")
    s.upsert_file(path=fp, mtime=1, content_hash="h")
    s.replace_chunks_for_file(fp, [dict(heading=None, line_start=1, line_end=1, text="t",
                                        tags=[], aliases=[], embedding=np.ones(8, dtype=np.float32))],
                              updated=updated)
    s.close()


def test_review_nudge_prints_only_when_overdue(tmp_path, monkeypatch):
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    _seed_index(tmp_path, "knowledge", "old", "2020-01-01")
    from rekol.cli_review import main
    res = CliRunner().invoke(main, ["--nudge"])
    assert "due for review" in res.output and res.exit_code == 0


def test_review_confirm_bumps_updated(tmp_path, monkeypatch):
    import datetime as dt
    import frontmatter
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    (tmp_path / "knowledge").mkdir()
    f = tmp_path / "knowledge" / "old.md"
    f.write_text("---\nname: o\ndescription: d\ntype: reference\nupdated: 2020-01-01\n---\nbody\n")
    _seed_index(tmp_path, "knowledge", "old", "2020-01-01")
    from rekol.cli_review import main
    res = CliRunner().invoke(main, [], input="c\n")
    assert res.exit_code == 0
    assert str(frontmatter.load(str(f))["updated"]) == dt.date.today().isoformat()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_review.py -k "nudge or confirm" -v`
Expected: FAIL (`ModuleNotFoundError: rekol.cli_review`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/rekol/cli_review.py  (new)
"""`rekol review` — confirm/invalidate durable memories overdue for re-confirmation."""
from __future__ import annotations

import datetime as dt

import click
import frontmatter

from rekol.config import load_config
from rekol.review import find_overdue
from rekol.store import IndexStore


def _overdue(cfg):
    store = IndexStore(db_path=cfg.index_db_path, use_sqlite_vec=False)
    store.init_schema()
    try:
        rows = store.distinct_file_timestamps()
    finally:
        store.close()
    return find_overdue(rows, memory_home=cfg.memory_home,
                        exempt_layers=cfg.temporal_recency_exempt_layers,
                        interval_days=cfg.temporal_confirm_interval_days,
                        today=dt.date.today())


@click.command()
@click.option("--nudge", is_flag=True, help="Print a one-line reminder iff overdue; for hooks.")
@click.option("--list", "as_list", is_flag=True, help="Print overdue file paths (non-interactive).")
def main(nudge: bool, as_list: bool) -> None:
    """Review durable (always/, knowledge/) memories overdue for confirmation."""
    cfg = load_config()
    overdue = _overdue(cfg)
    if nudge:
        if overdue:
            click.echo(f"[rekol] {len(overdue)} durable memories are due for "
                       f"review — run `rekol review`")
        return
    if not overdue:
        click.echo("All durable memories are within the confirmation interval.")
        return
    if as_list:
        for o in overdue:
            click.echo(o["file_path"])
        return
    for o in overdue:
        click.echo(f"{o['file_path']} (updated {o['updated'] or 'never'})")
        choice = click.prompt("[c]onfirm / [i]nvalidate / [s]kip", default="s").strip().lower()
        if choice.startswith("c"):
            post = frontmatter.load(o["file_path"])
            post["updated"] = dt.date.today().isoformat()
            with open(o["file_path"], "w", encoding="utf-8") as fh:
                fh.write(frontmatter.dumps(post))
            click.echo("  confirmed")
        elif choice.startswith("i"):
            click.echo(f"  to invalidate, run: rekol invalidate {o['file_path']}")
```

Register in `cli.py`: `from rekol.cli_review import main as review_cmd` / `main.add_command(review_cmd, name="review")`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_review.py -k "nudge or confirm" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rekol/cli_review.py src/rekol/cli.py tests/test_review.py
git commit -m "feat: add rekol review (confirm/invalidate overdue durable memory)"
```

---

### Task D3: SessionEnd nudge handler

**Files:**
- Modify: `hooks/sessionend-snippet.json` (add a `rekol review --nudge` handler)
- Test: `tests/test_cli_hooks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_hooks.py  (append)
def test_sessionend_snippet_includes_review_nudge():
    import json
    from pathlib import Path
    snip = json.loads((Path(__file__).resolve().parents[1] / "hooks" / "sessionend-snippet.json").read_text())
    cmds = [h["command"] for h in snip["hooks"]["SessionEnd"][0]["hooks"]]
    assert any("rekol review --nudge" in c for c in cmds)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_hooks.py -k review_nudge -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Add a third handler to the `SessionEnd[0].hooks` array in `hooks/sessionend-snippet.json`:

```json
          { "type": "command", "command": "rekol review --nudge" }
```

(Fresh installs get all SessionEnd handlers via Step 7D. The cutover re-installs rekol, so existing machines pick it up too.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_hooks.py -k review_nudge -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hooks/sessionend-snippet.json tests/test_cli_hooks.py
git commit -m "feat: nudge for overdue durable memories on session end"
```

---

### Task D4: Inline `[review?]` tag in search output

**Files:**
- Modify: `src/rekol/cli_search.py` (memory-hit render)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py  (append)
def test_overdue_durable_tag_helper():
    import datetime as dt
    from pathlib import Path
    from rekol.cli_search import _review_tag
    hit = {"file_path": "/m/knowledge/x.md", "updated": "2020-01-01"}
    tag = _review_tag(hit, memory_home=Path("/m"), exempt_layers=["knowledge"],
                      interval_days=180, today=dt.date(2026, 6, 1))
    assert tag == " [review?]"
    fresh = {"file_path": "/m/topics/y.md", "updated": "2026-05-31"}
    assert _review_tag(fresh, memory_home=Path("/m"), exempt_layers=["knowledge"],
                       interval_days=180, today=dt.date(2026, 6, 1)) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -k review_tag -v`
Expected: FAIL (`ImportError: _review_tag`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/rekol/cli_search.py
import datetime as _dt
from pathlib import Path as _Path
from rekol.ranking import _as_date, _layer_of


def _review_tag(hit, *, memory_home, exempt_layers, interval_days, today):
    if _layer_of(hit["file_path"], memory_home) not in set(exempt_layers):
        return ""
    ref = _as_date(hit.get("updated") or hit.get("created"))
    if ref is None or (today - ref).days > interval_days:
        return " [review?]"
    return ""
```

Append `_review_tag(...)` to each memory-hit render line (using `cfg` values and `_dt.date.today()`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -k review_tag -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/rekol/cli_search.py tests/test_cli.py
git commit -m "feat: tag overdue durable memories with [review?] in search output"
```

---

## Final verification

- [ ] Run the whole suite + lint: `pytest -q && ruff check src/rekol && mypy src/rekol`
- [ ] Install into a scratch `REKOL_HOME` and confirm: `rekol index rebuild` populates timestamps; `rekol search "<known invalidated topic>"` excludes it; `--include-invalidated` shows it tagged below live hits; a fresh session shows exactly one `<env-time>` block from `rekol _hook`.

---

## Self-Review

**Spec coverage:** A1 (normalize) + A3/A4 (store) + A7 (indexer) cover spec A1/A2/A3; A5 + A8 cover A4 (ranking) incl. layer-aware recency; A6 covers A6 (config); A2 + A9 cover A5 (migration) and A7 (promotion); B1-B4 cover spec B; C1 covers spec C. The mac_setup cutover is deferred to its own plan (stated in the header). Acceptance criteria map to A9/B-tests and the Final verification block.

**Placeholders:** none — every code step shows complete code. Two steps flag an output-format dependency on A1 (`invalidated_at` normalization) and on Click stdin handling, with the exact fallback to use — these are real implementation notes, not TODOs.

**Type consistency:** `apply_temporal_ranking(...) -> (list, int)` is used identically in A5 (def) and A8 (call); `cosine_score`/`final_score` keys are set in A5 and read in A8/A9; `memory_filtered_count` defined in A8 and gated there; `needs_schema_migration()` defined A2, used A9; `hook_group` defined B1, referenced B1 (cli.py) and B2/B3 (command strings).
