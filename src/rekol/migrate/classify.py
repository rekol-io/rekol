"""Classify a legacy memory file into a target memory layer.

Two-stage pipeline:

1. :func:`heuristic_classify` reads the YAML frontmatter ``type:`` field.
   If present and recognized (``feedback``, ``project``, ``reference``,
   ``always``, ``when``, ``topic``, ``knowledge``), maps to a layer
   deterministically.  Returns ``None`` when no frontmatter or unknown type.

2. :func:`classify_file` orchestrates: try heuristic first; fall back to the
   LLM wrapper (``call_claude_classifier``) when heuristic returns ``None``.
   On LLM failure or when ``allow_llm=False``, default to the ``knowledge``
   layer so content is searchable without claiming always-on budget.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

import frontmatter

from memory_tools.migrate.discover import LegacyFile
from memory_tools.migrate.llm import LLMUnavailable, call_claude_classifier

Layer = Literal["always", "when", "topic", "knowledge"]


# Map legacy frontmatter `type:` values to target layer.
LEGACY_TYPE_TO_LAYER: dict[str, Layer] = {
    "feedback": "when",
    "project": "topic",
    "reference": "topic",
    "always": "always",
    "when": "when",
    "topic": "topic",
    "knowledge": "knowledge",
}


# Filename prefixes that are purely legacy sort hints — strip during rename.
_LEGACY_FILENAME_PREFIXES = ("feedback_", "project_", "reference_")


@dataclass
class Classification:
    """Where and how to write a legacy file into $MEMORY_HOME."""

    layer: Layer
    target_filename: str
    frontmatter: dict
    body: str
    method: Literal["heuristic", "llm"]
    original_type: str | None = None


def target_filename_for(lf: LegacyFile, *, layer: Layer) -> str:
    """Compute the output filename, prefixed by project slug to avoid collisions.

    - Strips known legacy prefixes (``feedback_``, ``project_``, ``reference_``).
    - For ``when/`` layer, the convention is ``when-<slug>-<rest>.md``.
    - For other layers, ``<slug>-<rest>.md``.
    """
    name = lf.source_path.name
    # Strip legacy prefix.
    stem = name[:-3] if name.endswith(".md") else name
    for prefix in _LEGACY_FILENAME_PREFIXES:
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
            break
    slug = lf.project_slug
    if layer == "when":
        return f"when-{slug}-{stem}.md"
    return f"{slug}-{stem}.md"


def heuristic_classify(lf: LegacyFile) -> Classification | None:
    """Classify by frontmatter ``type:`` only.  Returns None when unable."""
    try:
        post = frontmatter.load(lf.source_path)
    except Exception:
        return None
    meta = dict(post.metadata or {})
    legacy_type = meta.get("type")
    if not isinstance(legacy_type, str):
        return None
    layer = LEGACY_TYPE_TO_LAYER.get(legacy_type)
    if layer is None:
        return None

    # Build rewritten frontmatter — type is the target layer, not the legacy type.
    today = dt.date.today().isoformat()
    new_meta: dict = {
        "name": meta.get("name", lf.source_path.stem),
        "description": meta.get("description", ""),
        "type": layer,
        "tags": meta.get("tags", []) or [],
        "aliases": meta.get("aliases", []) or [],
        "see_also": meta.get("see_also", []) or [],
        "created": str(meta.get("created", today)),
        "updated": today,
    }
    return Classification(
        layer=layer,
        target_filename=target_filename_for(lf, layer=layer),
        frontmatter=new_meta,
        body=post.content,
        method="heuristic",
        original_type=legacy_type,
    )


def build_classifier_prompt() -> str:
    """The classifier system prompt sent to claude -p."""
    return (
        "You are classifying a legacy memory file for migration into a "
        "layered memory system.\n\n"
        "Return ONLY a JSON object (no prose, no code fence required) with "
        "these keys:\n"
        '  - layer: one of "always", "when", "topic", "knowledge"\n'
        "  - filename: kebab-case filename ending in .md\n"
        "  - tags: list of short lowercase keyword strings (max ~8)\n"
        "  - aliases: list of alternate phrasings users might search for (max ~8)\n"
        "  - rationale: one-sentence explanation\n\n"
        "LAYER GUIDE:\n"
        "  - always: tiny identity-like facts loaded in EVERY session (<1KB). "
        "Rare — don't default here.\n"
        '  - when: activity-triggered rules ("before committing", '
        '"before scoping", "when preparing 1:1"). Filename starts with when-.\n'
        "  - topic: canonical noun-referenced sources (services, repos, schemas, "
        "URLs, people/team structures).\n"
        "  - knowledge: general durable facts that don't fit the above; "
        "searchable but not always-on.\n\n"
        "Use CURRENT INDEX below to match naming conventions already in use.\n"
    )


def classify_file(
    lf: LegacyFile,
    *,
    index_context: str,
    allow_llm: bool,
) -> Classification:
    """Orchestrate: heuristic first, LLM fallback, knowledge default."""
    heur = heuristic_classify(lf)
    if heur is not None:
        return heur

    body = lf.source_path.read_text(encoding="utf-8", errors="replace")

    if allow_llm:
        try:
            data = call_claude_classifier(
                prompt=build_classifier_prompt(),
                index_context=index_context,
                file_body=body,
            )
            layer = data.get("layer")
            if layer not in ("always", "when", "topic", "knowledge"):
                raise LLMUnavailable(f"bad layer from LLM: {layer!r}")
            filename = data.get("filename") or target_filename_for(
                lf,
                layer=layer,  # type: ignore[arg-type]
            )
            today = dt.date.today().isoformat()
            new_meta: dict = {
                "name": data.get("rationale", lf.source_path.stem)[:80]
                if not data.get("name")
                else data["name"],
                "description": data.get("description", data.get("rationale", ""))[:200],
                "type": layer,
                "tags": list(data.get("tags") or []),
                "aliases": list(data.get("aliases") or []),
                "see_also": [],
                "created": today,
                "updated": today,
            }
            # Strip original YAML frontmatter from body if present so the
            # rewritten file only has the NEW frontmatter.
            stripped_body = _strip_frontmatter(body)
            return Classification(
                layer=layer,  # type: ignore[arg-type]
                target_filename=filename,
                frontmatter=new_meta,
                body=stripped_body,
                method="llm",
            )
        except LLMUnavailable:
            pass  # fall through to knowledge default

    # Default: knowledge layer, preserve body as-is.
    today = dt.date.today().isoformat()
    return Classification(
        layer="knowledge",
        target_filename=target_filename_for(lf, layer="knowledge"),
        frontmatter={
            "name": lf.source_path.stem,
            "description": f"Legacy memory from {lf.project_slug}",
            "type": "knowledge",
            "tags": [],
            "aliases": [],
            "see_also": [],
            "created": today,
            "updated": today,
        },
        body=_strip_frontmatter(body),
        method="heuristic",
    )


def _strip_frontmatter(text: str) -> str:
    """Return the body portion of a markdown file — empty frontmatter survives."""
    try:
        post = frontmatter.loads(text)
        return post.content
    except Exception:
        return text
