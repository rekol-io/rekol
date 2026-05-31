#!/usr/bin/env bash
# rekol auto-reindex hook
#
# Wired into Claude Code's PostToolUse hook with matcher "Write|Edit"
# (see hooks/posttooluse-snippet.json). Reads the tool's input JSON on
# stdin and, if the edited file_path is under the memory home, runs
# `rekol index update` asynchronously so the vector index stays in sync
# with the markdown — without adding per-edit latency to the tool flow.
#
# The memory home is resolved from $REKOL_HOME, falling back to $MEMORY_HOME
# so existing installs keep working.
#
# Failure modes are silent by design (the hook must never block the tool):
#   - jq missing → exit 0
#   - file_path missing/empty → exit 0
#   - neither REKOL_HOME nor MEMORY_HOME set → exit 0
#   - edited file is not under the memory home → exit 0
#   - rekol not found on PATH or in $HOME/bin → exit 0
#
# Logs to ~/.claude/logs/rekol-reindex.log for audit / debugging.

set -u

input=$(cat)

# Bail if jq is missing — without it we can't parse the hook input
command -v jq >/dev/null 2>&1 || exit 0

file=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')

[ -z "$file" ] && exit 0

# REKOL_HOME is the primary data-directory variable; MEMORY_HOME is kept as a
# fallback so existing installs (which export MEMORY_HOME) keep working.
HOME_DIR="${REKOL_HOME:-${MEMORY_HOME:-}}"
[ -z "$HOME_DIR" ] && exit 0

case "$file" in
    "$HOME_DIR"/*) ;;
    *) exit 0 ;;
esac

# Resolve rekol: PATH first, then default ~/bin install location
INDEX_CMD="$(command -v rekol 2>/dev/null || true)"
if [ -z "$INDEX_CMD" ] && [ -x "$HOME/bin/rekol" ]; then
    INDEX_CMD="$HOME/bin/rekol"
fi
[ -z "$INDEX_CMD" ] && exit 0

log="$HOME/.claude/logs/rekol-reindex.log"
mkdir -p "$(dirname "$log")"

(
    {
        printf '\n--- %s reindex triggered by %s ---\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$file"
        "$INDEX_CMD" index update
    } >> "$log" 2>&1
) </dev/null &
disown $! 2>/dev/null || true

exit 0
