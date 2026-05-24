#!/usr/bin/env bash
# memory-tools auto-reindex hook
#
# Wired into Claude Code's PostToolUse hook with matcher "Write|Edit"
# (see hooks/posttooluse-snippet.json). Reads the tool's input JSON on
# stdin and, if the edited file_path is under $MEMORY_HOME, runs
# `memory-index update` asynchronously so the vector index stays in sync
# with the markdown — without adding per-edit latency to the tool flow.
#
# Failure modes are silent by design (the hook must never block the tool):
#   - jq missing → exit 0
#   - file_path missing/empty → exit 0
#   - MEMORY_HOME unset → exit 0
#   - edited file is not under MEMORY_HOME → exit 0
#   - memory-index not found on PATH or in $HOME/bin → exit 0
#
# Logs to ~/.claude/logs/memory-reindex.log for audit / debugging.

set -u

input=$(cat)

# Bail if jq is missing — without it we can't parse the hook input
command -v jq >/dev/null 2>&1 || exit 0

file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')

[ -z "$file" ] && exit 0
[ -z "${MEMORY_HOME:-}" ] && exit 0

case "$file" in
    "$MEMORY_HOME"/*) ;;
    *) exit 0 ;;
esac

# Resolve memory-index: PATH first, then default ~/bin install location
INDEX_CMD="$(command -v memory-index 2>/dev/null || true)"
if [ -z "$INDEX_CMD" ] && [ -x "$HOME/bin/memory-index" ]; then
    INDEX_CMD="$HOME/bin/memory-index"
fi
[ -z "$INDEX_CMD" ] && exit 0

log="$HOME/.claude/logs/memory-reindex.log"
mkdir -p "$(dirname "$log")"

(
    {
        printf '\n--- %s reindex triggered by %s ---\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$file"
        "$INDEX_CMD" update
    } >> "$log" 2>&1
) </dev/null &
disown $! 2>/dev/null || true

exit 0
