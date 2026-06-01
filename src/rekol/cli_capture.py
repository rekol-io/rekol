"""memory-capture CLI: interactive helper for writing new memory files.

v1 is a minimal assist: prompts for layer, file name, frontmatter fields,
then appends or creates the file and triggers an incremental reindex.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import click
import yaml

from rekol.chunker import chunk_body
from rekol.cli_common import guard_curated_schema
from rekol.config import load_config
from rekol.embeddings import get_embedder
from rekol.indexer import Indexer
from rekol.store import IndexStore

# Singular layer names used in CLI and frontmatter type field.
LAYERS = ("always", "when", "topic", "knowledge")

# Maps the CLI's singular layer name to the plural directory name on disk.
# The directory names are intentionally plural (e.g. ``topics/``) while the
# frontmatter ``type`` field and CLI option use the singular form.
_LAYER_DIR_MAP: dict[str, str] = {
    "always": "always",
    "when": "when",
    "topic": "topics",
    "knowledge": "knowledge",
}


# Default cosine-similarity threshold above which a candidate body is treated
# as a near-duplicate of an existing memory chunk.  0.85 is empirically a good
# balance for BGE-small embeddings — meaningful overlap, not just topical.
CONFLICT_THRESHOLD = 0.85


@click.command()
@click.option(
    "--layer",
    type=click.Choice(LAYERS),
    required=True,
    help="Memory layer: always | when | topic | knowledge.",
)
@click.option(
    "--file", "filename", required=True, help="Filename within the layer dir, e.g. 'prometheus.md'."
)
@click.option(
    "--name", required=True, help="Human-readable name for the frontmatter ``name`` field."
)
@click.option(
    "--description",
    required=True,
    help="One-line description for the frontmatter ``description`` field.",
)
@click.option("--tags", default="", help="Comma-separated list of tags.")
@click.option("--aliases", default="", help="Comma-separated list of search aliases.")
@click.option(
    "--body-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a file containing the markdown body.",
)
@click.option(
    "--body", default="", help="Inline markdown body (used when --body-file is not provided)."
)
@click.option(
    "--force", is_flag=True, default=False, help="Skip the near-duplicate check and write anyway."
)
@click.option(
    "--conflict-threshold",
    type=float,
    default=CONFLICT_THRESHOLD,
    show_default=True,
    help="Cosine-similarity threshold above which a candidate body is treated as a near-duplicate.",
)
@click.option(
    "--valid-from",
    default=None,
    help="ISO-8601 date this memory's facts started being true. Defaults to today.",
)
@click.option(
    "--project",
    "project_slug",
    default=None,
    help="Scope this memory to a project. Writes to "
    "projects/<slug>/<layer>/<file> instead of <layer>/<file>. "
    "Use a kebab-case slug like 'math-evolution-agent'.",
)
def main(
    layer: str,
    filename: str,
    name: str,
    description: str,
    tags: str,
    aliases: str,
    body_file: str | None,
    body: str,
    force: bool,
    conflict_threshold: float,
    valid_from: str | None,
    project_slug: str | None,
) -> None:
    """Write a new memory file and trigger an incremental reindex.

    Refuses to overwrite an existing file — rename or delete it first if
    you need to replace it.

    Also runs a semantic conflict check: if the candidate body is highly
    similar (cosine ≥ ``--conflict-threshold``) to an already-indexed chunk,
    the write is rejected with the conflicting file's path so the operator
    can decide whether to update the existing memory instead of creating a
    new one.  Use ``--force`` to bypass.
    """
    cfg = load_config()
    if project_slug:
        # Validate slug — kebab-case, no path separators, no leading dot.
        if not project_slug.replace("-", "").replace("_", "").isalnum():
            raise click.ClickException(
                f"--project slug must be alphanumeric/dash/underscore, got {project_slug!r}"
            )
        dest_dir = cfg.memory_home / "projects" / project_slug / _LAYER_DIR_MAP[layer]
    else:
        dest_dir = cfg.memory_home / _LAYER_DIR_MAP[layer]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    if dest.exists():
        raise click.ClickException(
            f"{dest} already exists. Delete or rename it before capturing a new file."
        )

    if body_file:
        body_text = Path(body_file).read_text()
    else:
        # Provide a minimal markdown skeleton when no body is supplied so the
        # file is immediately useful for search without manual editing.
        body_text = body or f"# {name}\n\n{description}\n"

    # Capture timestamps as timezone-aware ISO-8601 datetimes so chronological
    # ordering across same-day captures is unambiguous.  ``valid_from`` stays
    # date-only — its semantics are "what day did this fact start being true",
    # not "what moment did I capture it."
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    today_date = dt.date.today().isoformat()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    # Use yaml.dump so that values containing YAML-special characters (e.g.
    # "Prometheus: canonical source") are automatically quoted, keeping the
    # emitted frontmatter valid for parse_file() at reindex time.
    fm_data = {
        "name": name,
        "description": description,
        "type": layer,
        "tags": tag_list,
        "aliases": alias_list,
        "created": now_iso,
        "updated": now_iso,
        "valid_from": valid_from or today_date,
    }
    frontmatter = (
        "---\n"
        + yaml.dump(fm_data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        + "---\n\n"
    )

    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
    store.init_schema()
    guard_curated_schema(store)

    # Conflict check: chunk the candidate body the same way the indexer does,
    # then check each chunk against existing stored chunks.  Embedding the
    # full body and comparing against per-chunk stored embeddings would
    # underestimate similarity for any file larger than chunk_max_bytes — the
    # full-body vector and a chunk-slice vector represent different spans of
    # text, so cosine drops below the threshold even for true duplicates.
    # Skipped on --force, and silently no-ops when the index is empty.
    if not force:
        candidate_chunks = chunk_body(body_text, max_bytes=cfg.chunk_max_bytes)
        if candidate_chunks:
            texts_to_check = [f"{name}\n{c.text}" for c in candidate_chunks]
        else:
            # Empty body falls back to the indexer's empty-body convention:
            # one chunk consisting of "{name}: {description}".
            texts_to_check = [f"{name}: {description}"]

        # Stop at the first chunk that hits the threshold so the error message
        # points at the most-relevant overlap rather than the highest-scoring
        # in aggregate.
        for cand_text in texts_to_check:
            candidate_vec = embedder.embed(cand_text)
            hits = store.search(candidate_vec, top_k=1)
            if hits and hits[0]["score"] >= conflict_threshold:
                top = hits[0]
                raise click.ClickException(
                    f"near-duplicate memory exists "
                    f"(cosine={top['score']:.3f}, threshold={conflict_threshold:.2f})\n"
                    f"  existing: {top['file_path']}\n"
                    f"  heading:  {top['heading'] or '(no heading)'}\n"
                    f"  excerpt:  {top['text'][:200]!r}\n"
                    f"\nRecommend: update the existing file instead of creating "
                    f"a parallel memory.  Pass --force to write anyway."
                )

    dest.write_text(frontmatter + body_text)
    click.echo(f"wrote {dest}")

    idx = Indexer(
        memory_root=cfg.memory_home,
        store=store,
        embedder=embedder,
        chunk_max_bytes=cfg.chunk_max_bytes,
    )
    stats = idx.update()
    click.echo(f"indexed ({stats.files_indexed} updated)")


if __name__ == "__main__":
    sys.exit(main())
