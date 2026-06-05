#!/usr/bin/env bash
# rekol auto-reindex hook
#
# Wired into Claude Code's PostToolUse hook with matcher "Write|Edit|MultiEdit"
# (see hooks/posttooluse-snippet.json). Reads the tool's input JSON on
# stdin and, if the edited file_path is under the memory home, runs
# `rekol index update` asynchronously so the vector index stays in sync
# with the markdown — without adding per-edit latency to the tool flow.
#
# The memory home is resolved from $REKOL_HOME, falling back to $MEMORY_HOME
# so existing installs keep working.
#
# Concurrency (C3): a burst of edits (a multi-file refactor, or several
# MultiEdit hunks) would otherwise fire several overlapping `rekol index
# update` writers against the same index.db. They are serialized with a
# non-blocking lock on a stable lockfile: if a reindex is already running,
# this invocation exits 0 — the in-flight update will pick up the new edit
# (it walks the whole memory home, not just this file). A short debounce
# coalesces the leading edge of a burst into fewer runs.
#
# Failure modes are silent by design (the hook must never block the tool):
#   - jq missing → exit 0
#   - file_path missing/empty → exit 0
#   - neither REKOL_HOME nor MEMORY_HOME set → exit 0
#   - edited file is not under the memory home → exit 0
#   - rekol not found on PATH or in $HOME/bin → exit 0
#   - the reindex lock is already held → exit 0 (running update covers us)
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

# --- Serialization (C3) -----------------------------------------------------
# Stable per-memory-home lockfile so distinct $REKOL_HOME roots don't contend
# with each other but all edits under ONE root serialize onto one run. Keyed on
# a hash of $HOME_DIR; falls back to a sanitized basename if no hasher exists.
lock_dir="${TMPDIR:-/tmp}"
if command -v shasum >/dev/null 2>&1; then
    home_key="$(printf '%s' "$HOME_DIR" | shasum | cut -c1-16)"
elif command -v sha256sum >/dev/null 2>&1; then
    home_key="$(printf '%s' "$HOME_DIR" | sha256sum | cut -c1-16)"
else
    home_key="$(printf '%s' "$HOME_DIR" | tr -c 'A-Za-z0-9' '_')"
fi
lockfile="${lock_dir%/}/rekol-reindex-${home_key}.lock"

# The whole reindex runs in a detached background subshell so the hook returns
# immediately (never adds latency to the tool flow, never blocks the session).
run_reindex() {
    {
        printf '\n--- %s reindex triggered by %s ---\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$file"
        # Debounce: coalesce the leading edge of an edit burst. A run that
        # starts slightly later sees the later edits too, so we don't launch a
        # fresh `index update` for every keystroke-fast hunk.
        sleep 0.5
        "$INDEX_CMD" index update
    } >> "$log" 2>&1
}

if command -v flock >/dev/null 2>&1; then
    # flock path (Linux / util-linux): -n = non-blocking. If the lock is held by
    # an in-flight reindex, flock exits non-zero WITHOUT running the command, so
    # this invocation simply returns — the running update covers this edit.
    (
        flock -n 9 || exit 0
        run_reindex
    ) 9>"$lockfile" </dev/null &
    disown $! 2>/dev/null || true
else
    # Portable fallback (macOS has no flock): atomic mkdir is the classic POSIX
    # mutex. `mkdir` of an existing dir fails atomically, so only one invocation
    # wins the lock; the rest exit 0. The winner removes the lockdir on exit
    # (even on crash) via the trap so a killed reindex can't wedge the lock.
    lockdir="${lockfile%.lock}.lockd"
    (
        mkdir "$lockdir" 2>/dev/null || exit 0
        trap 'rmdir "$lockdir" 2>/dev/null || true' EXIT
        run_reindex
    ) </dev/null &
    disown $! 2>/dev/null || true
fi

exit 0
