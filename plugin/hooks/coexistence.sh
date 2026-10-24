#!/usr/bin/env bash
# Shared backend-coexistence detection for the plugin and install.sh.

REKOL_INSTALL_HOOK_MARKER="rekol:backend=install.sh:v1"

# Returns 0 when settings contains an install.sh-owned hook, 1 when it does not,
# and 2 when the file cannot be inspected safely.
settings_has_rekol_install_hooks() {
    local settings="$1"
    [ -f "$settings" ] || return 1
    command -v jq >/dev/null 2>&1 || return 2
    jq empty "$settings" >/dev/null 2>&1 || return 2
    jq -e --arg marker "$REKOL_INSTALL_HOOK_MARKER" '
      [ (.hooks // {} | .. | objects | .command? // empty)
        | select(type == "string") ]
      | any(.[];
          contains($marker)
          or (contains("REKOL.md") and contains("REKOL_HOME:-"))
          or (contains("rekol") and test("_hook (session-confidence|session-coverage|session-tasks|update-check|time-context|capture-nudge|record-stop|stop-failure-record)([ ;&]|$)"))
          or (contains("rekol") and contains("session-index --incremental"))
          or contains("auto-reindex.sh"))
    ' "$settings" >/dev/null 2>&1
}

# Returns 0 when an enabled rekol plugin entry exists, 1 when it does not, and
# 2 when the file cannot be inspected safely. Installed/cache state is not used:
# a plugin may remain installed while explicitly disabled.
settings_has_enabled_rekol_plugin() {
    local settings="$1"
    [ -f "$settings" ] || return 1
    command -v jq >/dev/null 2>&1 || return 2
    jq empty "$settings" >/dev/null 2>&1 || return 2
    jq -e '
      (.enabledPlugins // {})
      | to_entries
      | any(.key as $key | ($key | startswith("rekol@")) and (.value == true))
    ' "$settings" >/dev/null 2>&1
}

enabled_rekol_plugin_keys() {
    local settings="$1"
    jq -r '
      (.enabledPlugins // {})
      | to_entries[]
      | select(.key as $key | ($key | startswith("rekol@")) and (.value == true))
      | .key
    ' "$settings" 2>/dev/null
}
