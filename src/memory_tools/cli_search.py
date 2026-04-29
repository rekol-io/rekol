"""memory-search CLI: semantic search over the memory index."""
from __future__ import annotations

import json as json_mod
import sys

import click

from memory_tools.config import load_config
from memory_tools.embeddings import get_embedder
from memory_tools.store import IndexStore


@click.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--top", "top_k", default=5, show_default=True, type=int,
              help="Maximum number of results to return.")
@click.option("--json", "as_json", is_flag=True,
              help="Output results as a JSON array instead of human-readable text.")
def main(query: tuple[str, ...], top_k: int, as_json: bool) -> None:
    """Search memory semantically.

    QUERY is a natural-language phrase. Multiple words are joined with
    spaces before embedding, so quoting is optional:

    \b
        memory-search prometheus url
        memory-search "cassandra backup schedule" --top 10
    """
    cfg = load_config()
    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
    store.init_schema()
    query_text = " ".join(query)
    query_vec = embedder.embed(query_text)
    hits = store.search(query_vec, top_k=top_k)
    if as_json:
        click.echo(json_mod.dumps(
            [
                dict(
                    file_path=h["file_path"],
                    heading=h["heading"],
                    line_start=h["line_start"],
                    line_end=h["line_end"],
                    score=h["score"],
                    tags=h["tags"],
                    aliases=h["aliases"],
                    snippet=h["text"][:300],
                )
                for h in hits
            ],
            indent=2,
        ))
    else:
        for h in hits:
            heading_suffix = f" #{h['heading']}" if h["heading"] else ""
            click.echo(
                f"{h['score']:.3f}  {h['file_path']}{heading_suffix}"
                f"  (L{h['line_start']}-{h['line_end']})"
            )
            snippet_lines = h["text"].strip().splitlines()[:3]
            for line in snippet_lines:
                click.echo(f"    {line}")
            click.echo()


if __name__ == "__main__":
    sys.exit(main())
