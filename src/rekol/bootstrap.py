"""T3 (#41) bootstrap core: scope, ranking refinement, batching, classification.

The memory-bootstrap routine is the LLM **precision** half of the cold-start
distillation (EPIC #46): T2 (#40) recalls candidate instruction-bearing messages
from the indexed session corpus (no LLM, recall-oriented); T3 hands those to the
user's own Claude — driven by the ``rekol-bootstrap`` skill — to cluster,
classify into layers (``always``/``when``/``topics``/``knowledge``), correct
frontmatter, and (after a human approve/edit/skip gate) capture them. Nothing is
auto-written.

This module is the *harness-agnostic* code half — pure, testable, no LLM, no
network. It provides four things the skill and the ``rekol bootstrap`` CLI lean
on:

1. **Ranking refinement** (:func:`rerank_candidates`) applying T2's findings so
   the candidates handed to the assistant are higher-precision: user turns
   outrank assistant turns, and very long messages are length-penalised (terse
   standing instructions are the durable ones; sprawling messages are usually
   transient working context).
2. **Scope control** (:class:`ScopeFilter`) — a bounded-corpus default (recent N
   sessions / time window / chosen projects), widenable, serialisable into the
   resume checkpoint. Coordinates with T1's scope knob (same params).
3. **Batching** (:func:`plan_batches`) — split the in-scope candidates into
   deterministic per-project batches so the run is checkpointable and a reaped
   run resumes at batch granularity.
4. **Classification helpers** (:func:`classify_layer`,
   :func:`suggested_frontmatter`) — a conservative heuristic first guess at the
   layer + frontmatter the assistant refines, so even a degraded run (assistant
   unavailable) still produces a usable review file.
5. **Scalable review UX** (T3 v2, #62) for a big corpus (hundreds of proposals):
   :func:`group_by_layer` + a confidence-ranked, layer-grouped
   :func:`render_batch_review`; BULK-APPROVE (:func:`bulk_capture_commands`,
   :func:`extract_capture_commands_from_review`) to approve a whole batch in one
   action; and STOP-EARLY (:func:`truncate_to_top`) to review the top-N and defer
   the rest (resumably, never dropped).
6. **Always-on promotion** (:func:`promote_to_rekol_md`) — when a candidate is
   captured at the ``always`` layer, promote its pointer into ``REKOL.md`` (the
   always-on index re-injected every session); idempotent + budget-capped.
7. **End refine pass** (:func:`render_refine_summary`) — the affordance the skill
   uses for a final customize/refine pass over what the run captured.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rekol.corpus_propose import CorpusCandidate

# --- ranking refinement weights (T2 findings) ---------------------------------

# Multiplicative boost applied to user turns over assistant turns. T2 found user
# instructions ("we always X", "I prefer Y") are far higher-precision durable
# signals than assistant turns (which restate, summarise, or propose), so a user
# turn at the same base recall score should rank ahead.
USER_TURN_WEIGHT = 1.5
ASSISTANT_TURN_WEIGHT = 1.0

# Length penalty. Terse standing instructions are the durable ones; very long
# messages are usually transient working context (pasted logs, code, multi-step
# requests) that recall happens to match. Penalty grows with length past a soft
# floor and saturates so a long message is demoted, never zeroed out (it might
# still be a real instruction buried in context — the human review gate decides).
LENGTH_PENALTY_FLOOR_CHARS = 200
LENGTH_PENALTY_MAX = 0.5  # at most halve the score for an arbitrarily long message
LENGTH_PENALTY_SCALE_CHARS = 2000  # chars past the floor at which penalty saturates


def refined_score(candidate: CorpusCandidate) -> float:
    """Recompute a candidate's rank score with T2's precision findings applied.

    Starts from the recall score T2 assigned, multiplies by a role weight (user
    turns up, assistant turns flat), then applies a length penalty for messages
    longer than :data:`LENGTH_PENALTY_FLOOR_CHARS`. Pure; does not mutate the
    candidate.
    """
    role_weight = USER_TURN_WEIGHT if candidate.role == "user" else ASSISTANT_TURN_WEIGHT
    score = candidate.score * role_weight

    overflow = max(0, len(candidate.content) - LENGTH_PENALTY_FLOOR_CHARS)
    if overflow:
        # Linear ramp from 0 (at the floor) to LENGTH_PENALTY_MAX (at floor +
        # SCALE), clamped so arbitrarily long messages are demoted but not zeroed.
        ramp = LENGTH_PENALTY_MAX * overflow / LENGTH_PENALTY_SCALE_CHARS
        penalty = min(LENGTH_PENALTY_MAX, ramp)
        score *= 1.0 - penalty
    return score


def rerank_candidates(candidates: list[CorpusCandidate]) -> list[CorpusCandidate]:
    """Return ``candidates`` reordered by :func:`refined_score`, highest first.

    Pure: returns a new list, never mutates the input or the candidates. Ties
    break on the original recall score then session/uuid so the order is fully
    deterministic (a resume reranks identically). Drops nothing — reranking only
    reorders, so the human review gate still sees every recalled candidate.
    """
    return sorted(
        candidates,
        key=lambda c: (-refined_score(c), -c.score, c.session_id, c.message_uuid),
    )


# --- scope control ------------------------------------------------------------

# Bounded-corpus defaults. The first bootstrap over a deep history should NOT try
# to chew the entire corpus in one run — it would be slow and noisy. Default to a
# recent, bounded slice; the user widens explicitly (--scope-days / --all-time /
# --scope-projects). These mirror T1's scope-control knob (same params).
DEFAULT_SCOPE_DAYS = 90
DEFAULT_SCOPE_MAX_SESSIONS = 200


def project_slug_for_cwd(cwd: str | None) -> str:
    """Map a transcript ``cwd`` to a kebab-case project slug.

    The slug is the working dir's last path segment, lowercased with
    non-alphanumeric runs collapsed to single dashes — the same shape
    ``rekol capture --project`` expects. A missing/empty cwd maps to the stable
    ``"unknown"`` bucket so a candidate with no recorded cwd still batches
    cleanly rather than crashing the plan.
    """
    if not cwd:
        return "unknown"
    last = Path(cwd).name or "unknown"
    slug = re.sub(r"[^a-z0-9]+", "-", last.lower()).strip("-")
    return slug or "unknown"


@dataclass
class ScopeFilter:
    """Bounded-corpus scope control for a bootstrap run.

    ``projects`` restricts to candidates whose cwd maps to a listed slug (empty =
    all projects). ``days`` / ``max_sessions`` bound the corpus by recency and
    size; the SQL-level enumeration honours those, while :meth:`admits` is the
    in-memory project gate reused when reranking an already-fetched list.
    """

    projects: list[str] = field(default_factory=list)
    days: int | None = None
    max_sessions: int | None = None

    @classmethod
    def default(cls) -> ScopeFilter:
        """The bounded default scope: recent window, capped session count, all projects.

        The first run over a deep history stays fast and reviewable; the user
        widens explicitly.
        """
        return cls(projects=[], days=DEFAULT_SCOPE_DAYS, max_sessions=DEFAULT_SCOPE_MAX_SESSIONS)

    def admits(self, candidate: CorpusCandidate) -> bool:
        """True when ``candidate`` is within this scope's project filter.

        An empty ``projects`` list admits all projects (no filter). The recency /
        session-count bounds are applied at enumeration time (they need the whole
        corpus to rank), not here.
        """
        if not self.projects:
            return True
        return project_slug_for_cwd(candidate.cwd) in set(self.projects)

    def as_dict(self) -> dict:
        """Serialise to the dict the resume checkpoint stores.

        ``projects`` is sorted so two invocations listing the same projects in a
        different order produce an equal dict — the checkpoint's scope-match guard
        is then order-insensitive.
        """
        return {
            "projects": sorted(self.projects),
            "days": self.days,
            "max_sessions": self.max_sessions,
        }


def _parse_iso(ts: str) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp to an aware datetime, or None if unparseable.

    Transcript timestamps are ISO-8601, usually ``...Z``. ``fromisoformat`` did
    not accept a bare trailing ``Z`` before 3.11; we normalise it to ``+00:00``
    for safety. A naive datetime is treated as UTC so comparisons stay aware.
    Returns None (rather than raising) on garbage so a single malformed timestamp
    never aborts a whole scope pass.
    """
    raw = ts.strip().replace("Z", "+00:00") if ts.endswith("Z") else ts.strip()
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed


def apply_scope(
    candidates: list[CorpusCandidate],
    scope: ScopeFilter,
    *,
    now_iso: str,
) -> list[CorpusCandidate]:
    """Restrict ``candidates`` to ``scope`` (project + recency + session cap).

    Applied in-memory over a candidate list already recalled by T2, so the
    bootstrap reuses T2's recall unchanged and only narrows. Filters, in order:

    1. **Project** — drop candidates whose cwd-slug isn't in ``scope.projects``
       (empty list = no filter).
    2. **Recency** — when ``scope.days`` is set, drop candidates older than that
       many days before ``now_iso``. A candidate with an unparseable timestamp is
       kept (fail-open: a recall hit with a bad timestamp is still worth review).
    3. **Session cap** — when ``scope.max_sessions`` is set, keep only candidates
       belonging to the N most-recently-active distinct sessions (a session's
       recency = its newest candidate timestamp), bounding the corpus by size.

    Pure; returns a new list, input order preserved within the kept set. ``now_iso``
    is injected (not read from the clock) so the pass is deterministic and testable.
    """
    now = _parse_iso(now_iso) or dt.datetime.now(dt.UTC)

    kept = [c for c in candidates if scope.admits(c)]

    if scope.days is not None:
        cutoff = now - dt.timedelta(days=scope.days)
        # Fail-open on an unparseable timestamp: keep it rather than silently
        # dropping a real recall hit because its timestamp was malformed.
        kept = [c for c in kept if (_parse_iso(c.timestamp_iso) or now) >= cutoff]

    if scope.max_sessions is not None:
        # Rank distinct sessions by their newest candidate timestamp, keep the
        # top N session ids, then retain only candidates from those sessions.
        # An unparseable timestamp sorts LAST (oldest) here — unlike the days
        # filter's fail-open above — so a garbage timestamp can't masquerade as
        # the most-recent session and survive the cap over a genuinely-recent one.
        oldest = dt.datetime.min.replace(tzinfo=dt.UTC)
        newest_per_session: dict[str, dt.datetime] = {}
        for c in kept:
            ts = _parse_iso(c.timestamp_iso) or oldest
            if c.session_id not in newest_per_session or ts > newest_per_session[c.session_id]:
                newest_per_session[c.session_id] = ts
        top_sessions = {
            sid
            for sid, _ts in sorted(newest_per_session.items(), key=lambda kv: kv[1], reverse=True)[
                : scope.max_sessions
            ]
        }
        kept = [c for c in kept if c.session_id in top_sessions]

    return kept


# --- batching -----------------------------------------------------------------


@dataclass
class BootstrapBatch:
    """One unit of bootstrap work: a project's recalled candidates, reranked.

    Batch granularity is what the checkpoint tracks — a reaped run resumes at the
    first not-yet-completed batch, so the largest amount of reprocessing on a
    crash is one in-flight batch.
    """

    batch_id: str
    candidates: list[CorpusCandidate]


def plan_batches(candidates: list[CorpusCandidate]) -> list[BootstrapBatch]:
    """Group recalled candidates into deterministic per-project batches.

    Each batch's candidates are reranked (:func:`rerank_candidates`) so the
    highest-precision instruction-bearing turns sit at the top of the review
    file. Batch ids are ``project-<slug>`` and the returned list is sorted by id,
    so the plan order is fully deterministic — a resume rebuilds the identical
    plan and skips the batches the checkpoint marked done.

    An empty input yields an empty plan (a fresh install with nothing recalled
    plans zero batches rather than erroring).
    """
    by_slug: dict[str, list[CorpusCandidate]] = {}
    for candidate in candidates:
        slug = project_slug_for_cwd(candidate.cwd)
        by_slug.setdefault(slug, []).append(candidate)

    batches = [
        BootstrapBatch(batch_id=f"project-{slug}", candidates=rerank_candidates(cands))
        for slug, cands in by_slug.items()
    ]
    batches.sort(key=lambda b: b.batch_id)
    return batches


# --- classification helpers ---------------------------------------------------

# Singular layer names matching `rekol capture --layer`. "topic" is singular here
# (the CLI/frontmatter form) even though the on-disk dir is plural `topics/`.
_LAYER_ALWAYS = "always"
_LAYER_WHEN = "when"
_LAYER_TOPIC = "topic"
_LAYER_KNOWLEDGE = "knowledge"

# Canonical review order for the grouped review UX: MOST-AMBIENT-FIRST. Always-on
# items are re-injected into every session (the highest-stakes, most expensive
# layer), so they sit at the top of a batch's review file where they get the
# reviewer's full attention before they scroll into the long non-ambient tail.
# Knowledge (the safe non-ambient default, and usually the largest bucket on a
# big corpus) sinks to the bottom.
LAYER_REVIEW_ORDER: tuple[str, ...] = (_LAYER_ALWAYS, _LAYER_WHEN, _LAYER_TOPIC, _LAYER_KNOWLEDGE)

# Conservative first-guess classifiers, checked in priority order. These are a
# heuristic SEED the assistant refines — not the final word. The order matters:
# "when ..." is the most specific (activity-scoped), then location facts
# (noun-scoped topics), then bare imperatives (ambient always), and finally a
# safe non-ambient default (knowledge) for anything unclassifiable, so a wrong
# guess never silently lands in always-on memory.
_WHEN_RE = re.compile(r"^\s*(?:when|whenever|before|after|during)\b", re.IGNORECASE)
_LOCATION_RE = re.compile(
    r"\b(?:repo|project|code|files?|directory|dir)\b.*\blives?\b", re.IGNORECASE
)
_ALWAYS_RE = re.compile(r"^\s*(?:always|never|don'?t|do not|prefer|use)\b", re.IGNORECASE)

# Secret-shape signals. A line matching ANY of these is treated as secret-bearing
# and forced to the non-ambient ``knowledge`` layer BEFORE the always-on imperative
# check runs (a secret line that happens to start with "use/always/never/prefer"
# must never be nominated for always-on memory). These are conservative SHAPE
# matchers, not a secret scanner — they err toward catching a secret, since the
# cost of a missed one (leaking into ambient context) far outweighs a false
# positive (a non-secret demoted to knowledge, which the human review gate still
# sees). Anchored where it matters (key/token assignments, known vendor prefixes,
# explicit "password is X" / "token: X" phrasings).
_SECRET_RES: tuple[re.Pattern[str], ...] = (
    # OpenAI-style ``sk-...`` and GitHub ``ghp_/gho_/ghs_/ghr_...`` token prefixes.
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"\bgh[posru]_[A-Za-z0-9]{8,}"),
    # AWS access-key id (``AKIA...``) and the canonical secret-key env var name.
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bAWS_SECRET_ACCESS_KEY\b", re.IGNORECASE),
    # PEM block headers (private keys, certificates).
    re.compile(r"-----BEGIN\b"),
    # A ``KEY=<long value>`` / ``TOKEN=<value>`` / ``api_key=<value>`` assignment:
    # a name containing key/token/secret/password followed by ``=`` and a value.
    re.compile(
        r"\b[\w-]*(?:secret|token|api[_-]?key|password|passwd|access[_-]?key)[\w-]*\s*[=:]\s*\S+",
        re.IGNORECASE,
    ),
    # Natural-language secret disclosure: "password is X", "password: X", or a
    # bare "password <value>" ("my password hunter2") — any time the word
    # password/passwd/token/secret is followed by a literal value-looking token.
    re.compile(r"\b(?:password|passwd)\b(?:\s+is)?\s+\S+", re.IGNORECASE),
    re.compile(r"\btoken\s+is\s+\S+", re.IGNORECASE),
)


def _looks_secret_bearing(content: str) -> bool:
    """True when ``content`` matches any secret SHAPE (see :data:`_SECRET_RES`)."""
    return any(secret_re.search(content) for secret_re in _SECRET_RES)


def classify_layer(content: str) -> str:
    """Heuristically classify a candidate into a memory layer.

    A conservative SEED the assistant refines via the skill — deliberately simple
    (regex over phrasing), not an LLM. Priority order: secret-shaped lines are
    forced to the non-ambient ``knowledge`` layer FIRST (so a secret that leads
    with "use/always/never/prefer" can never be nominated for always-on memory),
    then activity-scoped ("when X …" → ``when``), location facts ("the repo lives
    in …" → ``topic``), bare standing imperatives ("always/never/prefer …" →
    ``always``), else the safe non-ambient default ``knowledge`` (so a
    misclassification never pollutes always-on memory).
    """
    # SECRET pre-check, BEFORE the always-on imperative match: an instruction
    # keyword in front of a secret ("use api_key=sk-…") must not promote it.
    if _looks_secret_bearing(content):
        return _LAYER_KNOWLEDGE
    if _WHEN_RE.search(content):
        return _LAYER_WHEN
    if _LOCATION_RE.search(content):
        return _LAYER_TOPIC
    if _ALWAYS_RE.search(content):
        return _LAYER_ALWAYS
    return _LAYER_KNOWLEDGE


def _suggested_name(content: str) -> str:
    """Derive a short human-readable name from a candidate's content.

    First sentence/clause, trimmed to a sane length — the assistant rewrites this,
    so it only needs to be a usable placeholder, never perfect.
    """
    first = re.split(r"[.\n!?]", content.strip(), maxsplit=1)[0].strip()
    return (first[:70] + "…") if len(first) > 70 else first or "untitled"


def suggested_frontmatter(content: str, *, layer: str | None = None) -> dict:
    """Build a seed frontmatter dict for a candidate (the assistant refines it).

    Carries the fields ``rekol capture`` requires (``type``, ``name``,
    ``description``) plus empty ``tags``/``aliases`` the assistant fills in. The
    ``type`` defaults to :func:`classify_layer`'s guess when ``layer`` is not
    given.
    """
    resolved_layer = layer or classify_layer(content)
    name = _suggested_name(content)
    return {
        "type": resolved_layer,
        "name": name,
        "description": content.strip(),
        "tags": [],
        "aliases": [],
    }


# --- scalable review UX: grouping + stop-early --------------------------------


def group_by_layer(candidates: list[CorpusCandidate]) -> dict[str, list[CorpusCandidate]]:
    """Bucket candidates by their suggested layer, confidence-ranked within each.

    The scalable-review primitive: on a big corpus a flat list of hundreds of
    proposals is unreviewable, so the review file is rendered GROUPED by suggested
    layer (so a reviewer can scan one layer at a time, or bulk-approve a whole
    group) and CONFIDENCE-RANKED within each group (highest :func:`refined_score`
    first, so the strongest candidates sit at the top of every group).

    Returns an ordered dict whose keys follow :data:`LAYER_REVIEW_ORDER`
    (most-ambient-first) — only layers with at least one candidate appear. Pure;
    drops nothing (every candidate lands in exactly one bucket), so the grouping
    never causes silent recall loss.
    """
    buckets: dict[str, list[CorpusCandidate]] = {}
    for candidate in candidates:
        layer = classify_layer(candidate.content)
        buckets.setdefault(layer, []).append(candidate)
    # Rank each group by confidence (refined score), then emit groups in the
    # canonical most-ambient-first order so the dict iteration order is stable.
    ordered: dict[str, list[CorpusCandidate]] = {}
    for layer in LAYER_REVIEW_ORDER:
        if layer in buckets:
            ordered[layer] = rerank_candidates(buckets[layer])
    # Defensive: any layer outside the canonical order (should not happen, since
    # classify_layer only returns known layers) still surfaces rather than being
    # silently dropped — appended after the canonical ones, confidence-ranked.
    for layer, cands in buckets.items():
        if layer not in ordered:
            ordered[layer] = rerank_candidates(cands)
    return ordered


def truncate_to_top(
    candidates: list[CorpusCandidate], top_n: int | None
) -> tuple[list[CorpusCandidate], list[CorpusCandidate]]:
    """Split candidates into the top-N to review now and the deferred remainder.

    The STOP-EARLY primitive: a huge batch can be reviewed in priority order N at
    a time, deferring the long tail to a later pass rather than forcing the whole
    batch in one sitting. Returns ``(kept, deferred)``; ``top_n=None`` (or a value
    at/above the length) keeps everything and defers nothing. Pure — input order
    is assumed already confidence-ranked, so the kept head is the highest-priority
    slice. The deferred tail is RETURNED, never dropped: the caller renders it
    below a divider so nothing is lost and a later pass can widen ``top_n``.
    """
    if top_n is None or top_n >= len(candidates):
        return list(candidates), []
    cut = max(0, top_n)
    return list(candidates[:cut]), list(candidates[cut:])


# --- incremental candidate writing --------------------------------------------


def _batch_review_path(pending_dir: Path, run_id: str, batch_id: str) -> Path:
    """Per-batch review file path.

    One file per (run, batch) so writes are atomic at batch granularity and a
    resume re-touches only the in-flight batch's file.
    """
    return pending_dir / f"bootstrap-{run_id}-{batch_id}.md"


# Sentinel marking where a newline was collapsed when embedding untrusted
# transcript content into the single-line markdown checklist item, so the
# operator can tell a multi-line candidate was flattened (the full content stays
# available via the cited source file). A visible glyph, not a bare space, so a
# line break isn't silently lost mid-sentence.
_NEWLINE_SENTINEL = " ↵ "


def _collapse_newlines(content: str) -> str:
    """Flatten ``content`` to a single line for safe markdown-checklist embedding.

    Untrusted transcript content can contain newlines (and lines shaped like
    ``- [ ]`` checkboxes); embedded verbatim it could spoof extra checklist items
    or break the review-file structure. Collapsing every CR/LF run to a single
    :data:`_NEWLINE_SENTINEL` keeps each candidate to exactly one checklist line.
    The untruncated original remains reachable via the cited source transcript.
    """
    return re.sub(r"[\r\n]+", _NEWLINE_SENTINEL, content.strip())


def _safe_capture_name(name: str) -> str:
    """Render a ``--name`` value safe to paste into a shell from a review file.

    The suggested ``rekol capture`` line is meant to be copy-pasteable, so raw
    transcript content with shell metacharacters (``"``, ``$(...)``, backticks,
    ``;``, ``|``) must never be embedded as the name — that would be a
    shell-injection vector. When the name is "plain" (letters/digits/space and a
    few safe punctuation marks) we keep it double-quoted; otherwise we substitute
    an explicit edit-me placeholder so nothing dangerous is ever rendered verbatim.
    """
    if re.fullmatch(r"[\w .,\-/]+", name):
        return f'"{name}"'
    return '"<edit before running>"'


def _capture_command(candidate: CorpusCandidate, *, layer: str, fm: dict) -> str:
    """Build the injection-safe ``rekol capture`` command string for a candidate.

    Single source of truth for BOTH the rendered ``capture:`` review line and the
    machine-readable :func:`bulk_capture_commands` plan, so a copy-pasted command
    and a bulk-approved one are byte-identical. The filename is a slug of the
    suggested name and the ``--name`` is injection-safe (shell metachars become an
    explicit placeholder), so the command is never a copy-paste shell hazard.
    """
    file_slug = re.sub(r"[^a-z0-9]+", "-", fm["name"].lower()).strip("-")[:50] or "candidate"
    return (
        f"rekol capture --layer {layer} --file {file_slug}.md "
        f'--name {_safe_capture_name(fm["name"])} --description "..."'
    )


def _render_candidate_block(candidate: CorpusCandidate) -> list[str]:
    """Render one candidate's review lines (checklist item + provenance + capture).

    Shared by the grouped review renderer for both the active head and the
    deferred tail, so every candidate is presented identically regardless of which
    section it lands in.
    """
    layer = classify_layer(candidate.content)
    fm = suggested_frontmatter(candidate.content, layer=layer)
    # Collapse newlines so a multi-line transcript turn can't spoof extra
    # checklist items / break the markdown (full content via the source file).
    return [
        f"- [ ] {_collapse_newlines(candidate.content)}",
        f"      suggested layer: **{layer}** · name: {fm['name']!r} · "
        f"confidence {refined_score(candidate):.2f}",
        f"      source: session `{candidate.session_id}` · "
        f"`{candidate.jsonl_path}`:{candidate.line_number} · "
        f"[{candidate.role}] {candidate.timestamp_iso} · "
        f"matched {candidate.matched_query!r} (score {candidate.score:.2f})",
        f"      capture: `{_capture_command(candidate, layer=layer, fm=fm)}`",
        "",
    ]


def render_batch_review(batch: BootstrapBatch, *, run_id: str, top_n: int | None = None) -> str:
    """Render the human-review markdown for one batch's candidates.

    Scalable review UX (#62 §6.5): candidates are GROUPED by suggested layer
    (most-ambient-first, so always-on items review first) and CONFIDENCE-RANKED
    within each group, so a batch of hundreds is scannable a layer at a time.
    Each candidate still carries a checklist item + provenance + a ready-to-edit
    ``rekol capture`` line the assistant presents for approve/edit/skip.

    Two scaling affordances the skill drives:

    - **BULK-APPROVE** — a header command (``rekol bootstrap --bulk-approve
      <batch>``) that emits a capture command for every reviewed candidate, so a
      reviewer can approve a whole batch/group in one action instead of N gates.
    - **STOP-EARLY** — when ``top_n`` is set, only the top-N highest-confidence
      candidates are rendered as active review items; the remainder is moved
      below a divider into a DEFERRED section (not dropped — a later pass widens
      ``--top``). Resumable: nothing is lost, the batch stays one checkpoint unit.

    The banner is explicit that nothing is auto-written — the EPIC's non-negotiable
    human-review gate.
    """
    total = len(batch.candidates)
    # Confidence-rank the whole batch first so STOP-EARLY keeps the strongest head
    # regardless of how the candidates were grouped per layer.
    ranked = rerank_candidates(batch.candidates)
    active, deferred = truncate_to_top(ranked, top_n)

    lines: list[str] = [
        f"# Bootstrap candidates — batch `{batch.batch_id}` (run {run_id})",
        "",
        "Distilled by the `rekol-bootstrap` skill from recalled session candidates. "
        "REVIEW-GATED: nothing here is auto-captured. For each item, approve / edit / "
        "skip, then run the shown `rekol capture` (the assistant does this for approved "
        "items). Check the cited transcript before trusting a candidate. Delete this "
        "file when the batch is done.",
        "",
        "BULK-APPROVE this whole batch in one action with "
        f"`rekol bootstrap --bulk-approve {batch.batch_id}` "
        "(prints a capture command per reviewed candidate; still review-gated — "
        "the assistant runs them only after the user approves the batch).",
        "",
    ]
    if total == 0:
        lines.append(f"## Candidates ({total})")
        lines.append("")
        lines.append("_(none recalled for this batch)_")
        lines.append("")
        return "\n".join(lines)

    if deferred:
        lines.append(
            f"## Candidates — reviewing top {len(active)} of {total} ({len(deferred)} deferred)"
        )
    else:
        lines.append(f"## Candidates ({total})")
    lines.append("")

    # Active head, grouped by layer (most-ambient-first), confidence-ranked within.
    for layer, group in group_by_layer(active).items():
        lines.append(f"### {layer} ({len(group)})")
        lines.append("")
        for candidate in group:
            lines.extend(_render_candidate_block(candidate))

    if deferred:
        lines.append("---")
        lines.append("")
        lines.append(f"## Deferred ({len(deferred)}) — STOP-EARLY tail, not yet reviewed")
        lines.append("")
        lines.append(
            "These lower-confidence candidates were set aside to keep this pass "
            "small. They are NOT dropped — re-run the batch with a larger "
            "`--top` (or no `--top`) to review them. Listed for transparency:"
        )
        lines.append("")
        for layer, group in group_by_layer(deferred).items():
            lines.append(f"### {layer} ({len(group)}) — deferred")
            lines.append("")
            for candidate in group:
                lines.extend(_render_candidate_block(candidate))
    return "\n".join(lines)


def bulk_capture_commands(batch: BootstrapBatch, *, top_n: int | None = None) -> list[str]:
    """Emit one ready-to-run ``rekol capture`` command per reviewed candidate.

    The BULK-APPROVE engine half: the skill calls this (via ``rekol bootstrap
    --bulk-approve <batch>``) to approve a whole batch/group in one action — it
    returns the capture plan as a list of command strings the skill runs after the
    user approves. It NEVER executes capture itself (review-gated — only the skill,
    on an approved item, runs the commands). Honors ``top_n`` (STOP-EARLY): only
    the reviewed head is emitted, so deferred candidates are not bulk-captured.
    Each command is injection-safe (see :func:`_capture_command`).
    """
    ranked = rerank_candidates(batch.candidates)
    active, _deferred = truncate_to_top(ranked, top_n)
    commands: list[str] = []
    for layer, group in group_by_layer(active).items():
        for candidate in group:
            fm = suggested_frontmatter(candidate.content, layer=layer)
            commands.append(_capture_command(candidate, layer=layer, fm=fm))
    return commands


# Heading prefix marking the STOP-EARLY deferred tail in a rendered review file.
# Capture lines AFTER this heading belong to deferred candidates and must NOT be
# bulk-approved (they weren't reviewed in this pass).
_DEFERRED_HEADING_PREFIX = "## Deferred"

# A rendered ``capture:`` review line, e.g. ``      capture: `rekol capture …` ``.
# Match the command between the backticks so a bulk-approve reads the SAME command
# the operator would copy-paste — the persisted review file is the source of truth.
_CAPTURE_LINE_RE = re.compile(r"capture:\s*`(rekol capture [^`]+)`")


def extract_capture_commands_from_review(review_text: str) -> list[str]:
    """Pull the active (non-deferred) ``rekol capture`` commands from a review file.

    BULK-APPROVE reads the PERSISTED review file (the on-disk source of truth the
    operator actually saw) rather than re-recalling the corpus — so the emitted
    commands are byte-identical to the ones in the file, even if recall later
    drifts. Capture lines under the STOP-EARLY ``## Deferred`` section are
    excluded: those candidates were set aside this pass and must not be
    bulk-captured. Returns the commands in file order (already confidence-ranked,
    grouped by layer).
    """
    commands: list[str] = []
    for line in review_text.splitlines():
        if line.startswith(_DEFERRED_HEADING_PREFIX):
            break  # everything past here is the deferred tail — not for bulk-approve
        match = _CAPTURE_LINE_RE.search(line)
        if match:
            commands.append(match.group(1))
    return commands


def write_batch_candidates(
    batch: BootstrapBatch, *, pending_dir: Path, run_id: str, top_n: int | None = None
) -> Path:
    """Write one batch's review file to ``pending_dir`` ATOMICALLY, returning its path.

    INCREMENTAL by design: each batch is written the moment it is planned/recalled
    (before the assistant processes it), so a run reaped after batch N has batches
    1..N already on disk as reviewable artifacts — the work is never lost. The
    path is deterministic per (run, batch), so re-running a batch (a resume that
    re-touches the last in-flight batch) overwrites in place rather than creating
    a duplicate file. Creates ``pending_dir`` if absent.

    ``top_n`` (STOP-EARLY) caps how many candidates are rendered as active review
    items; the remainder is written into a deferred section rather than dropped,
    so the artifact is still complete and resumable.

    The write mirrors ``save_state``'s atomic pattern (temp file + flush + fsync +
    ``os.replace``): a process killed mid-write leaves either the previous
    complete review file or the new complete one, never a torn file the operator
    (or a tool parsing the checklist) would misread.
    """
    pending_dir.mkdir(parents=True, exist_ok=True)
    out_path = _batch_review_path(pending_dir, run_id, batch.batch_id)
    body = render_batch_review(batch, run_id=run_id, top_n=top_n)
    tmp = out_path.with_name(out_path.name + ".tmp")
    # Write + flush + fsync the temp file before the rename so the bytes are on
    # disk when the atomic rename publishes them; otherwise a power loss could
    # rename an empty/partial inode into place.
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, out_path)
    return out_path


# --- always-on promotion → REKOL.md -------------------------------------------

# The hand-curated always-on index re-injected into every Claude session. When an
# approved candidate is captured at the ``always`` layer, its pointer/trigger line
# is promoted here so the new always-on file is actually reachable ambiently
# (capturing the file alone wouldn't surface it — REKOL.md is the index).
REKOL_MD_FILENAME = "REKOL.md"

# Heading under which bootstrap-promoted always-on pointers are collected, so the
# operator can see (and prune) what the bootstrap added without it being mixed
# into their hand-written sections.
_PROMOTED_SECTION_HEADING = "## Bootstrapped always-on"


@dataclass
class PromoteResult:
    """Outcome of a :func:`promote_to_rekol_md` call.

    ``promoted`` is True only when a NEW pointer line was actually written;
    ``reason`` carries why a promotion was a no-op (already present, or refused for
    budget) so the caller can report it honestly rather than claiming a write that
    didn't happen.
    """

    promoted: bool
    reason: str | None = None


def promote_to_rekol_md(
    memory_home: Path,
    *,
    name: str,
    rel_path: str,
    trigger: str,
    budget_bytes: int | None = None,
) -> PromoteResult:
    """Add an always-on pointer line for a captured ``always`` file into REKOL.md.

    REKOL.md is the hand-curated always-on INDEX re-injected into every session;
    capturing an ``always/`` file does not by itself make it ambient — its pointer
    must land here. This promotes one captured always-on item to a trigger-phrased
    pointer line under a dedicated "Bootstrapped always-on" section.

    Idempotent: a pointer for ``rel_path`` already present is not re-added
    (``promoted=False``). Creates REKOL.md (with the always-on banner) when absent,
    so a fresh install still gets a valid index. Budget-capped: always-on context
    is expensive (re-injected every session), so a promotion that would grow the
    file past ``budget_bytes`` is REFUSED and surfaced (``promoted=False`` + a
    reason), never silently written over the cap. ``name``/``trigger`` are
    flattened to a single line so untrusted transcript text can't inject a heading
    or extra list item into the index structure.
    """
    rekol_md = memory_home / REKOL_MD_FILENAME
    existing = rekol_md.read_text(encoding="utf-8") if rekol_md.exists() else ""

    # Already promoted? Idempotent no-op (the link target is the stable identity).
    if f"]({rel_path})" in existing:
        return PromoteResult(promoted=False, reason="already present in REKOL.md")

    # Flatten untrusted name/trigger so an embedded newline / ``## heading`` /
    # ``- [ ]`` fragment can't break out of the single pointer line.
    safe_name = _collapse_newlines(name) or "always-on memory"
    safe_trigger = _collapse_newlines(trigger)
    pointer = (
        f"- **{safe_trigger}** — [{safe_name}]({rel_path})"
        if safe_trigger
        else f"- [{safe_name}]({rel_path})"
    )

    new_text = _append_under_section(existing, _PROMOTED_SECTION_HEADING, pointer)

    # Budget guard BEFORE writing: refuse to grow the always-on index past the cap.
    if budget_bytes is not None and len(new_text.encode("utf-8")) > budget_bytes:
        return PromoteResult(
            promoted=False,
            reason=(
                f"promotion refused: REKOL.md would exceed the always-on budget "
                f"({budget_bytes} bytes). Prune an existing always-on pointer or "
                f"keep this item non-ambient."
            ),
        )

    rekol_md.parent.mkdir(parents=True, exist_ok=True)
    rekol_md.write_text(new_text, encoding="utf-8")
    return PromoteResult(promoted=True)


def _append_under_section(text: str, heading: str, line: str) -> str:
    """Return ``text`` with ``line`` appended under ``heading`` (heading added if absent).

    Keeps bootstrap-promoted pointers collected under one heading rather than
    scattered, so the operator can audit/prune what the bootstrap added. When the
    file lacks the always-on banner entirely (a fresh install), a minimal banner is
    prepended so the resulting REKOL.md is still a valid always-on index.
    """
    body = text
    if not body.strip():
        body = (
            "# Memory Index (always-on)\n\n"
            "This file is re-injected into every Claude session. Pointers below "
            "are phrased as triggers — read the referenced file when the trigger "
            "matches the user's request.\n"
        )
    if heading in body:
        # Insert the new pointer at the END of the existing section (just before
        # the next heading or EOF), so promotions accumulate in capture order.
        lines = body.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.strip() == heading)
        end = start + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        # Trim trailing blanks inside the section, append the pointer, restore one
        # blank separator before the next heading.
        section_end = end
        while section_end > start + 1 and not lines[section_end - 1].strip():
            section_end -= 1
        lines[section_end:section_end] = [line]
        return "\n".join(lines) + ("\n" if body.endswith("\n") else "")
    # No section yet: append a fresh one at EOF.
    suffix = "" if body.endswith("\n") else "\n"
    return f"{body}{suffix}\n{heading}\n\n{line}\n"


# --- end refine pass ----------------------------------------------------------


def render_refine_summary(captured_items: list[dict]) -> str:
    """Render the end-of-bootstrap refine-pass summary the skill drives.

    The END REFINE PASS affordance (#62): after the run captures items, the skill
    offers the user one final customize/refine pass. This builds the summary it
    presents — what was captured this run, grouped by layer with each file's path
    — so the user can audit and the skill can drive edits (rename, re-layer, tweak
    frontmatter) before declaring the bootstrap done.

    ``captured_items`` is a list of dicts with ``layer``, ``name``, ``rel_path``.
    An empty run yields a graceful "nothing captured" summary rather than a blank
    string, so the skill always has something coherent to show.
    """
    lines: list[str] = [
        "# Bootstrap refine pass",
        "",
        "The bootstrap captured the items below. This is your final customize/"
        "refine pass — tell the assistant to rename, re-layer, edit frontmatter, "
        "or remove any of these before they settle into memory (you can change "
        "this anytime).",
        "",
    ]
    if not captured_items:
        lines.append("_Nothing was captured this run — nothing to refine._")
        lines.append("")
        return "\n".join(lines)

    by_layer: dict[str, list[dict]] = {}
    for item in captured_items:
        by_layer.setdefault(str(item.get("layer", "knowledge")), []).append(item)
    for layer in LAYER_REVIEW_ORDER:
        group = by_layer.get(layer)
        if not group:
            continue
        lines.append(f"## {layer} ({len(group)})")
        lines.append("")
        for item in group:
            name = _collapse_newlines(str(item.get("name", "")).strip()) or "(unnamed)"
            rel_path = str(item.get("rel_path", "")).strip()
            lines.append(f"- **{name}** — `{rel_path}`")
        lines.append("")
    return "\n".join(lines)
