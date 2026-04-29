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

from memory_tools.config import load_config
from memory_tools.embeddings import get_embedder
from memory_tools.indexer import Indexer
from memory_tools.store import IndexStore


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


@click.command()
@click.option("--layer", type=click.Choice(LAYERS), required=True,
              help="Memory layer: always | when | topic | knowledge.")
@click.option("--file", "filename", required=True,
              help="Filename within the layer dir, e.g. 'prometheus.md'.")
@click.option("--name", required=True,
              help="Human-readable name for the frontmatter ``name`` field.")
@click.option("--description", required=True,
              help="One-line description for the frontmatter ``description`` field.")
@click.option("--tags", default="",
              help="Comma-separated list of tags.")
@click.option("--aliases", default="",
              help="Comma-separated list of search aliases.")
@click.option("--body-file", type=click.Path(exists=True, dir_okay=False),
              help="Path to a file containing the markdown body.")
@click.option("--body", default="",
              help="Inline markdown body (used when --body-file is not provided).")
def main(
    layer: str,
    filename: str,
    name: str,
    description: str,
    tags: str,
    aliases: str,
    body_file: str | None,
    body: str,
) -> None:
    """Write a new memory file and trigger an incremental reindex.

    Refuses to overwrite an existing file — rename or delete it first if
    you need to replace it.
    """
    cfg = load_config()
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

    today = dt.date.today().isoformat()
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
        "created": today,
        "updated": today,
    }
    frontmatter = (
        "---\n"
        + yaml.dump(fm_data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        + "---\n\n"
    )
    dest.write_text(frontmatter + body_text)
    click.echo(f"wrote {dest}")

    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
    store.init_schema()
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
