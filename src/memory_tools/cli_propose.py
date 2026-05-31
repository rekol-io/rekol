"""memory-propose CLI: surface candidate memories from a notes file.

Conservative auto-capture path: read a markdown file or stdin, find lines
that look like memorable statements (TODO/Decision/Note/correction/preference
markers), check them against existing memory via the semantic index, and write
a proposal file under ``$MEMORY_HOME/pending-review/<timestamp>.md`` for the
operator to review.

Does NOT call any LLM by default — heuristics only.  Privacy and cost
implications of running an LLM over session transcripts are out of scope for
v1.  The output is a checklist the user can act on manually with
``memory-capture``.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

import click

from memory_tools.config import load_config
from memory_tools.embeddings import get_embedder
from memory_tools.store import IndexStore

# Common leading whitespace + optional bullet marker, applied uniformly.
_LEAD = r"^\s*(?:[-*]\s+)?"

# Regex for candidate-memory lines.  Each pattern targets a phrasing that
# typically signals a durable fact worth remembering across sessions.
_CANDIDATE_PATTERNS = [
    # Imperative reminders
    re.compile(_LEAD + r"(?:remember|don't forget|note|todo|fyi)[:\s]\s*(.+)$", re.IGNORECASE),
    # Decisions
    re.compile(_LEAD + r"(?:decision|chose|going with)[:\s]\s*(.+)$", re.IGNORECASE),
    # Preferences and corrections
    re.compile(_LEAD + r"(?:prefer|use|don't use|avoid)\s+(.+)$", re.IGNORECASE),
    # Direct user-style "you forgot" / "I told you" corrections
    re.compile(_LEAD + r"(?:you forgot|i told you|i said)\s+(.+)$", re.IGNORECASE),
]


# Threshold above which a candidate is considered already-captured (skip).
DUPLICATE_THRESHOLD = 0.80


def extract_candidates(text: str) -> list[str]:
    """Return distinct candidate-memory lines from ``text``."""
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        for pat in _CANDIDATE_PATTERNS:
            m = pat.match(line)
            if m:
                snippet = m.group(1).strip().rstrip(".")
                if snippet and snippet.lower() not in seen:
                    seen.add(snippet.lower())
                    out.append(snippet)
                break
    return out


@click.command()
@click.option(
    "--input-file",
    "-i",
    type=click.Path(exists=True, dir_okay=False),
    help="Read notes from a file. If omitted, reads stdin.",
)
@click.option("--quiet", is_flag=True, help="Suppress the summary line.")
def main(input_file: str | None, quiet: bool) -> None:
    """Scan notes for candidate memories; write a proposal for review.

    Output goes to ``$MEMORY_HOME/pending-review/<timestamp>.md``.  Each
    candidate is annotated with the most-similar existing memory (if any) so
    the operator can decide whether to capture as new, update an existing
    memory, or drop the candidate.
    """
    cfg = load_config()
    text = Path(input_file).read_text(encoding="utf-8") if input_file else sys.stdin.read()

    candidates = extract_candidates(text)
    if not candidates:
        if not quiet:
            click.echo("no candidates found")
        return

    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
    store.init_schema()

    pending_dir = cfg.memory_home / "pending-review"
    pending_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    proposal = pending_dir / f"{ts}.md"

    lines: list[str] = [
        f"# Candidate memories from {ts}",
        "",
        "Review each item.  For ones worth keeping, run `memory-capture` "
        "with the suggested layer.  Delete this file when done.",
        "",
    ]

    new_count = 0
    dup_count = 0
    for cand in candidates:
        vec = embedder.embed(cand)
        hits = store.search(vec, top_k=1)
        if hits and hits[0]["score"] >= DUPLICATE_THRESHOLD:
            dup_count += 1
            lines.append(
                f"- ~~{cand}~~ — already captured "
                f"({hits[0]['score']:.2f}: `{hits[0]['file_path']}`)"
            )
        else:
            new_count += 1
            lines.append(f"- [ ] {cand}")
    lines.append("")

    proposal.write_text("\n".join(lines))

    if not quiet:
        click.echo(f"wrote {proposal} ({new_count} new, {dup_count} already-captured)")


if __name__ == "__main__":
    sys.exit(main())
