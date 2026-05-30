"""docs_convert: turn an arbitrary text-file tree into synthetic Claude Code JSONL.

The synthetic transcripts are written under ``claude_projects_dir`` so the
existing ``claude-session-index`` ingester surfaces them in ``memory-search``
with no changes to the ingester or store.
"""

# Extensions read as plain UTF-8 text. .jsonl is intentionally NOT here — its
# extension collides with this tool's own output format and source .jsonl files
# are not transcript-shaped (they would parse as malformed transcript rows).
TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {"md", "txt", "log", "csv", "tsv", "json", "py", "sh", "xml", "yaml", "yml"}
)
