#!/usr/bin/env bash
# rekol installer — idempotent; safe to rerun on an already-bootstrapped machine.
#
# Required environment variable:
#   REKOL_HOME — path to the memory root directory (local; sync it however you like)
#                (MEMORY_HOME is accepted as a fallback for existing installs)
#
# Optional flags:
#   --dry-run       print actions without executing them
#   --no-hook       skip SessionStart hook installation (settings.json only)
#   --no-skill      skip Claude skill installation
#   --no-shellrc    skip ~/.zshrc edits (PATH + REKOL_HOME export)
#   --test-mode     shorthand for --no-hook --no-skill --no-shellrc (use in tests)
#   --tools-home P  override default ~/.local/share/rekol
#   --bin-dir P     override default ~/bin
#   --migrate       opt in to importing legacy ~/.claude/projects/*/memory/ content
#
# Per Bash standard: using [[ ]] for conditionals, printf instead of echo -e,
# local for function-scoped vars, SCREAMING_SNAKE_CASE for constants.
# Note: set -euo pipefail used here per explicit installer design requirement.
set -euo pipefail

# Resolve installer's own directory so it works from any working directory
COMPONENT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
readonly COMPONENT_DIR

# Defaults — overridable via flags.
# Install location reads REKOL_TOOLS_HOME first, then MEMORY_TOOLS_HOME as a
# fallback so an existing install dir is still found, else the rekol default.
TOOLS_HOME_DEFAULT="${REKOL_TOOLS_HOME:-${MEMORY_TOOLS_HOME:-$HOME/.local/share/rekol}}"
BIN_DIR_DEFAULT="$HOME/bin"
readonly SETTINGS_JSON="$HOME/.claude/settings.json"
readonly SKILL_BASE="$HOME/.claude/skills"
readonly ZSHRC="$HOME/.zshrc"

# Mutable config (set by flag parsing)
DRY_RUN=0
DO_HOOK=1
DO_SKILL=1
DO_SHELLRC=1
DO_MIGRATE=0
TEST_MODE=0
TOOLS_HOME="$TOOLS_HOME_DEFAULT"
BIN_DIR="$BIN_DIR_DEFAULT"

# --- Helpers ---

say() {
  printf '%s\n' "$*"
}

# Prints a dry-run notice or executes the given command string.
# All side-effecting operations go through this so --dry-run is reliable.
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    say "DRY-RUN: $*"
  else
    eval "$@"
  fi
}

# Appends a timestamped entry to the install journal (no-op in dry-run mode).
log_journal() {
  if [[ "$DRY_RUN" == "0" ]]; then
    printf '%s\n' "$*" >> "$JOURNAL"
  fi
}

# --- Argument parsing ---

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)    DRY_RUN=1;                            shift ;;
    --no-hook)    DO_HOOK=0;                            shift ;;
    --no-skill)   DO_SKILL=0;                           shift ;;
    --no-shellrc) DO_SHELLRC=0;                         shift ;;
    --migrate)    DO_MIGRATE=1;                         shift ;;
    --test-mode)  DO_HOOK=0; DO_SKILL=0; DO_SHELLRC=0; TEST_MODE=1; shift ;;
    --tools-home) TOOLS_HOME="$2";                      shift 2 ;;
    --bin-dir)    BIN_DIR="$2";                         shift 2 ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

# --- Pre-flight: REKOL_HOME (or MEMORY_HOME fallback) must be set ---

if [[ -z "${REKOL_HOME:-}" && -z "${MEMORY_HOME:-}" ]]; then
  printf 'error: neither REKOL_HOME nor MEMORY_HOME is set. Point REKOL_HOME at any directory (sync it via Dropbox/iCloud/git/Syncthing or keep it local) (MEMORY_HOME is accepted as a fallback).\n' >&2
  exit 2
fi

# REKOL_HOME is the primary data-directory variable; MEMORY_HOME is kept as a
# fallback so existing installs (which export MEMORY_HOME) keep working. All
# mkdir/journal/seed/index logic below operates on this resolved path.
RESOLVED_HOME="${REKOL_HOME:-$MEMORY_HOME}"
readonly RESOLVED_HOME

# --- Journal setup ---
# Journal records every mutation for post-install auditing.
# The journal lives in RESOLVED_HOME/.install-logs/ so it does not pollute the
# root and does not interfere with the is_empty check for template seeding.

TS="$(date +%Y%m%d-%H%M%S)"
readonly TS
JOURNAL_DIR="${RESOLVED_HOME}/.install-logs"
JOURNAL="${JOURNAL_DIR}/.install-journal-${TS}.log"

run "mkdir -p '${RESOLVED_HOME}'"
run "mkdir -p '${JOURNAL_DIR}'"
if [[ "$DRY_RUN" == "0" ]]; then
  : > "$JOURNAL"
  printf 'rekol install %s\n' "$TS" >> "$JOURNAL"
fi

# =============================================================================
# Step 1 — Python venv
# =============================================================================

run "mkdir -p '${TOOLS_HOME}'"

if [[ ! -d "${TOOLS_HOME}/.venv" ]]; then
  # Prefer an interpreter built with --enable-loadable-sqlite-extensions so
  # sqlite-vec can actually load (vec0 KNN). python.org's macOS installer
  # ships extension loading DISABLED by deliberate policy, so the system
  # default `python3` on a Mac that has that installer wins PATH and the
  # venv inherits the broken sqlite3 module — sqlite-vec silently falls
  # back to a numpy cosine scan, with a warning on every search.
  #
  # Preference order:
  #   1. uv-managed Python (python-build-standalone — always has extensions)
  #   2. Homebrew Python (built with extensions)
  #   3. Fall back to whatever python3 is first on PATH
  PY3="$(command -v python3 || true)"
  if command -v uv >/dev/null 2>&1; then
    uv_py="$(uv python find 2>/dev/null || true)"
    if [[ -n "$uv_py" ]] && [[ -x "$uv_py" ]]; then
      PY3="$uv_py"
    fi
  fi
  if [[ -z "$PY3" || ! -x "$PY3" ]] || ! "$PY3" -c "import sqlite3; c=sqlite3.connect(':memory:'); assert hasattr(c, 'enable_load_extension')" >/dev/null 2>&1; then
    for candidate in /opt/homebrew/bin/python3 /usr/local/opt/python@3/bin/python3; do
      if [[ -x "$candidate" ]] && "$candidate" -c "import sqlite3; c=sqlite3.connect(':memory:'); assert hasattr(c, 'enable_load_extension')" >/dev/null 2>&1; then
        PY3="$candidate"
        break
      fi
    done
  fi
  say "creating venv at ${TOOLS_HOME}/.venv using ${PY3}"
  run "'${PY3}' -m venv '${TOOLS_HOME}/.venv'"
  log_journal "CREATED venv ${TOOLS_HOME}/.venv interpreter=${PY3}"
fi

say "installing/upgrading rekol into venv"
run "'${TOOLS_HOME}/.venv/bin/pip' install -U pip"
run "'${TOOLS_HOME}/.venv/bin/pip' install -U -e '${COMPONENT_DIR}'"

# =============================================================================
# Step 2 — ~/bin shim
# =============================================================================
# The eight legacy per-command shims are collapsed into a single `rekol` shim
# that delegates to the unified Click group (rekol.cli) in the venv.

run "mkdir -p '${BIN_DIR}'"

rekol_shim_src="${COMPONENT_DIR}/bin/rekol"
rekol_shim_dst="${BIN_DIR}/rekol"

# Back up any existing non-symlink file to avoid silently overwriting it
if [[ -e "$rekol_shim_dst" ]] && [[ ! -L "$rekol_shim_dst" ]]; then
  rekol_shim_backup="${rekol_shim_dst}.bak-${TS}"
  say "backing up existing ${rekol_shim_dst} → ${rekol_shim_backup}"
  run "mv '${rekol_shim_dst}' '${rekol_shim_backup}'"
  log_journal "BACKED-UP ${rekol_shim_dst} -> ${rekol_shim_backup}"
fi

run "ln -sf '${rekol_shim_src}' '${rekol_shim_dst}'"
log_journal "SYMLINK ${rekol_shim_dst} -> ${rekol_shim_src}"

# =============================================================================
# Step 3 — PATH in ~/.zshrc
# =============================================================================

# Skipped when --no-shellrc or --test-mode is in effect — avoids polluting the
# real ~/.zshrc during acceptance tests and CI runs.
if [[ "$DO_SHELLRC" == "1" ]]; then
  # Only appends when BIN_DIR is not already referenced — avoids duplicate entries
  if ! grep -qs "${BIN_DIR}" "${ZSHRC}" 2>/dev/null; then
    say "adding ${BIN_DIR} to PATH in ${ZSHRC}"
    run "printf '\n# rekol\nexport PATH=\"%s:\$PATH\"\n' '${BIN_DIR}' >> '${ZSHRC}'"
    log_journal "APPENDED-PATH ${ZSHRC}"
  fi
fi

# =============================================================================
# Step 4 — REKOL_HOME export in ~/.zshrc
# =============================================================================

# Skipped when --no-shellrc or --test-mode is in effect.
# Exports REKOL_HOME (the primary data-directory variable); guarded so it is not
# re-added on reruns.
if [[ "$DO_SHELLRC" == "1" ]]; then
  if ! grep -qs "^export REKOL_HOME=" "${ZSHRC}" 2>/dev/null; then
    say "adding REKOL_HOME export to ${ZSHRC}"
    run "printf 'export REKOL_HOME=\"%s\"\n' '${RESOLVED_HOME}' >> '${ZSHRC}'"
    log_journal "APPENDED-REKOL_HOME ${ZSHRC}"
  fi
fi

# =============================================================================
# Step 5 — Seed $REKOL_HOME from template/ if empty
# =============================================================================
# Uses -A to include hidden files; the is_empty check covers newly created dirs.

is_empty_memory_home() {
  local dir_path="$1"
  # Returns true (0) when the memory home contains no user content.
  # Excludes .install-logs/ which is created by the installer itself before
  # this check runs, so its presence does not indicate a prior user install.
  local file_count
  file_count="$(
    find "${dir_path}" -mindepth 1 -maxdepth 1 \
      ! -name '.install-logs' \
      ! -name '.dropboxignore' \
      2>/dev/null | wc -l
  )"
  [[ "$file_count" -eq 0 ]]
}

if is_empty_memory_home "${RESOLVED_HOME}"; then
  say "${RESOLVED_HOME} is empty — seeding from template/"
  run "cp -R '${COMPONENT_DIR}/template/'* '${RESOLVED_HOME}/'"
  # Rename every *.example file to its real name (strips the .example suffix)
  run "find '${RESOLVED_HOME}' -name '*.example' -print0 \
    | xargs -0 -I {} sh -c 'mv \"\$1\" \"\${1%.example}\"' _ {}"
  log_journal "SEEDED ${RESOLVED_HOME} from template"
else
  say "${RESOLVED_HOME} is non-empty — skipping template seeding (safe)"
fi

# =============================================================================
# Step 6 — Claude skill install
# =============================================================================

if [[ "$DO_SKILL" == "1" ]]; then
  # Install both the canonical `rekol` skill and the `memory` back-compat shim.
  # Claude Code derives the /<name> trigger from the directory name and has no
  # aliases field, so the shim directory is what keeps `/memory` working.
  for skill_name in rekol memory; do
    skill_dst_dir="${SKILL_BASE}/${skill_name}"
    run "mkdir -p '${skill_dst_dir}'"

    local_skill_src="${COMPONENT_DIR}/skill/${skill_name}/skill.md"
    local_skill_dst="${skill_dst_dir}/skill.md"

    # Back up only when content differs — avoids churn on repeated installs
    if [[ -f "$local_skill_dst" ]] && ! cmp -s "${local_skill_src}" "${local_skill_dst}"; then
      local_skill_backup="${local_skill_dst}.bak-${TS}"
      say "backing up existing ${local_skill_dst} → ${local_skill_backup}"
      run "cp '${local_skill_dst}' '${local_skill_backup}'"
      log_journal "BACKED-UP ${local_skill_dst} -> ${local_skill_backup}"
    fi

    run "cp '${local_skill_src}' '${local_skill_dst}'"
    log_journal "INSTALLED skill ${local_skill_dst}"
  done
fi

# =============================================================================
# Step 7 — SessionStart hook merge into ~/.claude/settings.json
# =============================================================================

if [[ "$DO_HOOK" == "1" ]]; then
  run "mkdir -p '$(dirname "${SETTINGS_JSON}")'"

  if [[ ! -f "${SETTINGS_JSON}" ]]; then
    run "printf '{}' > '${SETTINGS_JSON}'"
    log_journal "CREATED ${SETTINGS_JSON}"
  fi

  # Always back up settings.json before mutating it
  local_settings_backup="${SETTINGS_JSON}.bak-${TS}"
  run "cp '${SETTINGS_JSON}' '${local_settings_backup}'"
  log_journal "BACKED-UP ${SETTINGS_JSON} -> ${local_settings_backup}"

  SNIPPET="${COMPONENT_DIR}/hooks/sessionstart-snippet.json"

  if ! command -v jq >/dev/null 2>&1; then
    say "jq not found; printing hook snippet — merge manually into ${SETTINGS_JSON}"
    cat "${SNIPPET}"
  else
    # Detect whether the exact hook command is already present to maintain idempotency
    HAS_HOOK="$(
      jq --slurpfile snip "${SNIPPET}" '
        (.hooks.SessionStart // []) as $cur
        | ($snip[0].hooks.SessionStart[0].hooks[0].command) as $cmd
        | any($cur[]; .hooks // [] | any(.command == $cmd))
      ' "${SETTINGS_JSON}" 2>/dev/null || printf 'false'
    )"

    if [[ "$HAS_HOOK" == "true" ]]; then
      say "SessionStart hook already present — no-op"
    else
      local_tmp="${SETTINGS_JSON}.tmp.$$"
      run "jq --slurpfile snip '${SNIPPET}' \
        '.hooks.SessionStart = ((.hooks.SessionStart // []) + \$snip[0].hooks.SessionStart)' \
        '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
      log_journal "MERGED SessionStart hook into ${SETTINGS_JSON}"
    fi
  fi
fi

# =============================================================================
# Step 7.5 — REKOL_HOME into ~/.claude/settings.json env block
# =============================================================================
# Claude Code sessions do NOT source ~/.zshrc, so the shell-level REKOL_HOME
# export from Step 4 isn't visible to the SessionStart hook subshell or to the
# Bash tool.  Empirically verified: only settings.json's `env` block propagates
# to subprocess shells — settings.local.json's `env` block does NOT.  Without
# this step the SessionStart hook prints "$REKOL_HOME not configured" and the
# entire memory system is dark.  Idempotent: if the key is already set to the
# current value, no-op.

if [[ "$DO_HOOK" == "1" ]]; then
  if ! command -v jq >/dev/null 2>&1; then
    say "jq not found; skipping Claude settings.json env update — add env.REKOL_HOME manually"
  else
    HAS_REKOL_HOME="$(
      jq --arg want "${RESOLVED_HOME}" \
        '(.env.REKOL_HOME // "") == $want' \
        "${SETTINGS_JSON}" 2>/dev/null || printf 'false'
    )"

    if [[ "$HAS_REKOL_HOME" == "true" ]]; then
      say "REKOL_HOME already in ${SETTINGS_JSON} env — no-op"
    else
      # Independent backup before this step's mutation.  Step 7's earlier
      # backup of ${SETTINGS_JSON} reflects the file BEFORE the hook merge,
      # so it does not capture the post-hook state we are about to modify.
      local_settings_env_backup="${SETTINGS_JSON}.bak-env-${TS}"
      run "cp '${SETTINGS_JSON}' '${local_settings_env_backup}'"
      log_journal "BACKED-UP ${SETTINGS_JSON} -> ${local_settings_env_backup}"

      local_tmp="${SETTINGS_JSON}.tmp.$$"
      run "jq --arg val '${RESOLVED_HOME}' \
        '.env = ((.env // {}) + {REKOL_HOME: \$val})' \
        '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
      log_journal "SET env.REKOL_HOME in ${SETTINGS_JSON}"
      say "added REKOL_HOME to ${SETTINGS_JSON} env"
    fi
  fi
fi

# =============================================================================
# Step 7B — Install auto-reindex hook script
# =============================================================================
# Symlinks the auto-reindex shell script into ${TOOLS_HOME}/hooks/ so the
# PostToolUse hook (Step 7C) can reference a stable path.  Symlink-back-to-repo
# mirrors the ~/bin shim pattern from Step 2 — edits to the script in the repo
# are picked up live with no reinstall.

run "mkdir -p '${TOOLS_HOME}/hooks'"

local_autoreindex_src="${COMPONENT_DIR}/hooks/auto-reindex.sh"
local_autoreindex_dst="${TOOLS_HOME}/hooks/auto-reindex.sh"

# Back up any pre-existing non-symlink at the destination (mirrors Step 2)
if [[ -e "$local_autoreindex_dst" ]] && [[ ! -L "$local_autoreindex_dst" ]]; then
  local_autoreindex_backup="${local_autoreindex_dst}.bak-${TS}"
  say "backing up existing ${local_autoreindex_dst} → ${local_autoreindex_backup}"
  run "mv '${local_autoreindex_dst}' '${local_autoreindex_backup}'"
  log_journal "BACKED-UP ${local_autoreindex_dst} -> ${local_autoreindex_backup}"
fi

run "ln -sf '${local_autoreindex_src}' '${local_autoreindex_dst}'"
log_journal "SYMLINK ${local_autoreindex_dst} -> ${local_autoreindex_src}"

# =============================================================================
# Step 7C — PostToolUse auto-reindex hook merge into settings.json
# =============================================================================
# Wires the auto-reindex script (Step 7B) into Claude Code's PostToolUse event
# with matcher "Write|Edit".  Every time the agent edits a file under
# $REKOL_HOME, the script fires `rekol index update` asynchronously so the
# vector DB stays in sync without per-edit latency.
#
# Idempotency: skips the merge if the exact command is already present in any
# existing PostToolUse hook entry (same pattern as Step 7).

if [[ "$DO_HOOK" == "1" ]]; then
  SNIPPET_PTU="${COMPONENT_DIR}/hooks/posttooluse-snippet.json"

  if ! command -v jq >/dev/null 2>&1; then
    say "jq not found; printing PostToolUse snippet — merge manually into ${SETTINGS_JSON}"
    cat "${SNIPPET_PTU}"
  else
    HAS_PTU_HOOK="$(
      jq --slurpfile snip "${SNIPPET_PTU}" '
        (.hooks.PostToolUse // []) as $cur
        | ($snip[0].hooks.PostToolUse[0].hooks[0].command) as $cmd
        | any($cur[]; .hooks // [] | any(.command == $cmd))
      ' "${SETTINGS_JSON}" 2>/dev/null || printf 'false'
    )"

    if [[ "$HAS_PTU_HOOK" == "true" ]]; then
      say "PostToolUse auto-reindex hook already present — no-op"
    else
      # Independent backup for this step (Step 7's backup predates the env
      # mutation in Step 7.5 and would not reflect post-7.5 state)
      local_settings_ptu_backup="${SETTINGS_JSON}.bak-ptu-${TS}"
      run "cp '${SETTINGS_JSON}' '${local_settings_ptu_backup}'"
      log_journal "BACKED-UP ${SETTINGS_JSON} -> ${local_settings_ptu_backup}"

      local_tmp="${SETTINGS_JSON}.tmp.$$"
      run "jq --slurpfile snip '${SNIPPET_PTU}' \
        '.hooks.PostToolUse = ((.hooks.PostToolUse // []) + \$snip[0].hooks.PostToolUse)' \
        '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
      log_journal "MERGED PostToolUse auto-reindex hook into ${SETTINGS_JSON}"
    fi
  fi
fi

# =============================================================================
# Step 7D — SessionEnd transcript-index hook merge into settings.json
# =============================================================================
# Wires sessionend-snippet.json into Claude Code's SessionEnd event so that
# every time a session ends, `rekol session-index --incremental` reindexes the
# just-finished transcript (and the snippet's first handler prints a capture
# reminder).  This is what makes transcript memory CONTINUOUS + AUTOMATIC:
# without it, sessions only index when the user runs `rekol session-index` by
# hand.  Default-on under DO_HOOK (consistent with Steps 7 and 7C); --no-hook
# disables all hook wiring.
#
# Idempotency: skips the merge if the snippet's session-index command is already
# present in any existing SessionEnd hook entry. We key on the second handler
# (`rekol session-index ...`) rather than the first (the capture-reminder echo),
# because the reminder wording is the most likely thing to change between
# versions — keying on it would re-append the whole block (and a second
# session-index handler) after any reminder tweak.

if [[ "$DO_HOOK" == "1" ]]; then
  SNIPPET_SE="${COMPONENT_DIR}/hooks/sessionend-snippet.json"

  if ! command -v jq >/dev/null 2>&1; then
    say "jq not found; printing SessionEnd snippet — merge manually into ${SETTINGS_JSON}"
    cat "${SNIPPET_SE}"
  else
    HAS_SE_HOOK="$(
      jq --slurpfile snip "${SNIPPET_SE}" '
        (.hooks.SessionEnd // []) as $cur
        | ($snip[0].hooks.SessionEnd[0].hooks[1].command) as $cmd
        | any($cur[]; .hooks // [] | any(.command == $cmd))
      ' "${SETTINGS_JSON}" 2>/dev/null || printf 'false'
    )"

    if [[ "$HAS_SE_HOOK" == "true" ]]; then
      say "SessionEnd transcript-index hook already present — no-op"
    else
      # Independent backup for this step (earlier backups predate the 7.5/7C
      # mutations and would not reflect the current on-disk state).
      local_settings_se_backup="${SETTINGS_JSON}.bak-se-${TS}"
      run "cp '${SETTINGS_JSON}' '${local_settings_se_backup}'"
      log_journal "BACKED-UP ${SETTINGS_JSON} -> ${local_settings_se_backup}"

      local_tmp="${SETTINGS_JSON}.tmp.$$"
      run "jq --slurpfile snip '${SNIPPET_SE}' \
        '.hooks.SessionEnd = ((.hooks.SessionEnd // []) + \$snip[0].hooks.SessionEnd)' \
        '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
      log_journal "MERGED SessionEnd transcript-index hook into ${SETTINGS_JSON}"
      say "added SessionEnd transcript-index hook to ${SETTINGS_JSON}"
    fi
  fi
fi

# =============================================================================
# Step 7E — UserPromptSubmit time-context hook merge into settings.json
# =============================================================================
# Wires userpromptsubmit-snippet.json so each turn gets an <env-time> block from
# REKOL's own hook (replacing the external mac_setup time component). Idempotency
# is keyed on the `rekol _hook time-context` command. Double-injection guard: if
# a legacy mac_setup inject-time-context.sh hook is still present we do NOT add a
# second injector and we warn — run the mac_setup uninstall, then re-run install.

if [[ "$DO_HOOK" == "1" ]]; then
  SNIPPET_UPS="${COMPONENT_DIR}/hooks/userpromptsubmit-snippet.json"

  if ! command -v jq >/dev/null 2>&1; then
    say "jq not found; printing UserPromptSubmit snippet — merge manually into ${SETTINGS_JSON}"
    cat "${SNIPPET_UPS}"
  else
    HAS_LEGACY_TIME="$(
      jq '[.hooks.UserPromptSubmit[]?.hooks[]?.command] | any(. | test("inject-time-context.sh"))' \
        "${SETTINGS_JSON}" 2>/dev/null || printf 'false'
    )"
    HAS_REKOL_TIME="$(
      jq '[.hooks.UserPromptSubmit[]?.hooks[]?.command] | any(. == "rekol _hook time-context")' \
        "${SETTINGS_JSON}" 2>/dev/null || printf 'false'
    )"

    if [[ "$HAS_LEGACY_TIME" == "true" ]]; then
      say "Legacy mac_setup time hook detected — rekol's time hook was NOT installed. Run the mac_setup uninstall, then re-run 'rekol install'."
    elif [[ "$HAS_REKOL_TIME" == "true" ]]; then
      say "UserPromptSubmit time-context hook already present — no-op"
    else
      local_settings_ups_backup="${SETTINGS_JSON}.bak-ups-${TS}"
      run "cp '${SETTINGS_JSON}' '${local_settings_ups_backup}'"
      log_journal "BACKED-UP ${SETTINGS_JSON} -> ${local_settings_ups_backup}"

      local_tmp="${SETTINGS_JSON}.tmp.$$"
      run "jq --slurpfile snip '${SNIPPET_UPS}' \
        '.hooks.UserPromptSubmit = ((.hooks.UserPromptSubmit // []) + \$snip[0].hooks.UserPromptSubmit)' \
        '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
      log_journal "MERGED UserPromptSubmit time-context hook into ${SETTINGS_JSON}"
      say "added UserPromptSubmit time-context hook to ${SETTINGS_JSON}"
    fi
  fi
fi

# =============================================================================
# Step 7F — Stop record-stop hook merge into settings.json
# =============================================================================
# Wires stop-snippet.json so the assistant-completion timestamp is recorded for
# the next turn's elapsed deltas. Idempotency keyed on `rekol _hook record-stop`.

if [[ "$DO_HOOK" == "1" ]]; then
  SNIPPET_STOP="${COMPONENT_DIR}/hooks/stop-snippet.json"

  if ! command -v jq >/dev/null 2>&1; then
    say "jq not found; printing Stop snippet — merge manually into ${SETTINGS_JSON}"
    cat "${SNIPPET_STOP}"
  else
    HAS_STOP_HOOK="$(
      jq '[.hooks.Stop[]?.hooks[]?.command] | any(. == "rekol _hook record-stop")' \
        "${SETTINGS_JSON}" 2>/dev/null || printf 'false'
    )"

    if [[ "$HAS_STOP_HOOK" == "true" ]]; then
      say "Stop record-stop hook already present — no-op"
    else
      local_settings_stop_backup="${SETTINGS_JSON}.bak-stop-${TS}"
      run "cp '${SETTINGS_JSON}' '${local_settings_stop_backup}'"
      log_journal "BACKED-UP ${SETTINGS_JSON} -> ${local_settings_stop_backup}"

      local_tmp="${SETTINGS_JSON}.tmp.$$"
      run "jq --slurpfile snip '${SNIPPET_STOP}' \
        '.hooks.Stop = ((.hooks.Stop // []) + \$snip[0].hooks.Stop)' \
        '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
      log_journal "MERGED Stop record-stop hook into ${SETTINGS_JSON}"
      say "added Stop record-stop hook to ${SETTINGS_JSON}"
    fi
  fi
fi

# =============================================================================
# Step 7G — ensure the durable-memory review nudge is wired into SessionEnd
# =============================================================================
# Step 7D merges the whole SessionEnd block on a fresh install (which now
# includes a `rekol review --nudge` handler). But on a machine that already had
# the earlier two-handler SessionEnd block, Step 7D no-ops (keyed on the
# session-index command), so the newer nudge handler would never be added. This
# step adds the nudge as its own SessionEnd entry iff it is absent — idempotent
# on fresh installs (where Step 7D already added it).

if [[ "$DO_HOOK" == "1" ]] && command -v jq >/dev/null 2>&1; then
  HAS_NUDGE="$(
    jq '[.hooks.SessionEnd[]?.hooks[]?.command] | any(. == "rekol review --nudge")' \
      "${SETTINGS_JSON}" 2>/dev/null || printf 'false'
  )"
  if [[ "$HAS_NUDGE" == "true" ]]; then
    say "SessionEnd review-nudge handler already present — no-op"
  else
    local_settings_nudge_backup="${SETTINGS_JSON}.bak-nudge-${TS}"
    run "cp '${SETTINGS_JSON}' '${local_settings_nudge_backup}'"
    log_journal "BACKED-UP ${SETTINGS_JSON} -> ${local_settings_nudge_backup}"
    local_tmp="${SETTINGS_JSON}.tmp.$$"
    run "jq '.hooks.SessionEnd = ((.hooks.SessionEnd // []) + [{matcher: \"\", hooks: [{type: \"command\", command: \"rekol review --nudge\"}]}])' \
      '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
    log_journal "MERGED SessionEnd review-nudge handler into ${SETTINGS_JSON}"
    say "added SessionEnd review-nudge handler to ${SETTINGS_JSON}"
  fi
fi

# =============================================================================
# Step 8 — Sync-ignore file for the local vector index (best-effort)
# =============================================================================
# Keep the local vector index out of any file-sync (it is machine-specific,
# rebuildable, and would conflict across machines). We write `.dropboxignore`;
# for other sync tools exclude `.index/` yourself.

if [[ ! -f "${RESOLVED_HOME}/.dropboxignore" ]]; then
  say "writing ${RESOLVED_HOME}/.dropboxignore to keep machine-specific .index/ out of sync (for other sync tools, exclude .index/ yourself)"
  run "printf '.index/\n.writing.lock\n' > '${RESOLVED_HOME}/.dropboxignore'"
  log_journal "CREATED ${RESOLVED_HOME}/.dropboxignore"
fi

# =============================================================================
# Step 8.5 — Local git repo for audit trail (opt-in via rekol.config.yaml (memory.config.yaml as fallback))
# =============================================================================
# When rekol.config.yaml (memory.config.yaml as fallback) has `git_track: true`, init a local git repo in
# $REKOL_HOME so memory captures and edits get a real commit history.  This
# is the only meaningful recovery path from Dropbox conflict copies (which
# silently overwrite without auditable diffs).  No remote is configured — the
# git repo is local-only by design.

# rekol.config.yaml is the current name; fall back to memory.config.yaml so an
# existing root created by an older install is still read.
CONFIG_YAML="${RESOLVED_HOME}/rekol.config.yaml"
[[ -f "${CONFIG_YAML}" ]] || CONFIG_YAML="${RESOLVED_HOME}/memory.config.yaml"
# Strip the key, leading whitespace, any trailing inline comment, and any
# trailing whitespace.  Without the comment-stripping pass, a config like
# `git_track: true  # for audit` would parse to "true # for audit" and the
# `== "true"` test below would silently fail, leaving git_track effectively
# disabled with no diagnostic.
GIT_TRACK="$(
  if [[ -f "${CONFIG_YAML}" ]]; then
    # `|| true`: a config with no git_track line makes grep exit 1, which under
    # `set -o pipefail` would fail the substitution and abort the whole install
    # at this step. Swallow only the no-match so an absent key yields an empty
    # value — which the `== "true"` test below treats as git-tracking-off (the
    # safe default). A real sed/head error still surfaces.
    { grep -E '^git_track:' "${CONFIG_YAML}" || true; } \
      | sed -E 's/^git_track:[[:space:]]*//' \
      | sed -E 's/[[:space:]]*#.*$//' \
      | sed -E 's/[[:space:]]*$//' \
      | head -1
  fi
)"
if [[ "${GIT_TRACK}" == "true" ]]; then
  if [[ ! -d "${RESOLVED_HOME}/.git" ]]; then
    say "initializing local git repo at ${RESOLVED_HOME}"
    run "git -C '${RESOLVED_HOME}' init -q -b main"
    log_journal "GIT-INIT ${RESOLVED_HOME}"
  fi
  if [[ ! -f "${RESOLVED_HOME}/.gitignore" ]]; then
    say "writing ${RESOLVED_HOME}/.gitignore"
    run "printf '.index/\n.writing.lock\n.install-logs/\n' > '${RESOLVED_HOME}/.gitignore'"
    log_journal "CREATED ${RESOLVED_HOME}/.gitignore"
  fi
  # Initial commit if the repo has no commits yet.  Set local user.email/name
  # so the commit succeeds without requiring a global git config.  Use direct
  # `git` calls (not `run "..."`) so a non-zero exit propagates: `run()` uses
  # `eval` and silently swallows failures, which would otherwise make the
  # install journal misreport a failed commit as successful.
  if ! git -C "${RESOLVED_HOME}" rev-parse --verify HEAD >/dev/null 2>&1; then
    say "creating initial git commit in ${RESOLVED_HOME}"
    if [[ "$DRY_RUN" == "1" ]]; then
      say "DRY-RUN: git -C '${RESOLVED_HOME}' add -A && git ... commit -m 'memory: initial commit'"
    elif git -C "${RESOLVED_HOME}" add -A \
        && git -C "${RESOLVED_HOME}" \
            -c user.email='rekol@localhost' \
            -c user.name='rekol installer' \
            commit -q -m 'memory: initial commit'; then
      log_journal "GIT-INITIAL-COMMIT ${RESOLVED_HOME}"
    else
      say "WARNING: git initial commit failed — check 'git status' in ${RESOLVED_HOME}"
      log_journal "WARN-GIT-INITIAL-COMMIT-FAILED ${RESOLVED_HOME}"
    fi
  fi
fi

# =============================================================================
# Step 9 — Build or update the vector index
# =============================================================================
# Run rebuild on first install; update on subsequent runs.

if [[ -f "${RESOLVED_HOME}/.index/index.db" ]]; then
  say "existing index found — running rekol index update"
  # Invoke the single venv entrypoint directly so the correct venv is used even
  # when --tools-home overrides the default ~/.local/share/rekol path.
  run "'${TOOLS_HOME}/.venv/bin/rekol' index update"
else
  say "no index found — running rekol index rebuild"
  run "'${TOOLS_HOME}/.venv/bin/rekol' index rebuild"
fi

# =============================================================================
# Step 9.5 — Backfill existing Claude Code transcripts into the sessions index
# =============================================================================
# Step 9 builds only the curated memory index.  This step seeds the sibling
# sessions.db by reindexing all existing ~/.claude/projects/**/*.jsonl, so a
# fresh install has searchable transcript history IMMEDIATELY rather than only
# accumulating it from the next SessionEnd onward.  Embeddings are on by default
# (semantic search), reusing the model already downloaded by Step 9 — so the
# marginal cost is inference only.  The CLI self-gates: it no-ops when
# session_search_enabled=false and exits non-fatally when the projects dir is
# absent.  Wrapped non-fatal so a transcript hiccup never aborts the install
# (set -euo pipefail is in effect).  Skipped in --test-mode (would otherwise
# walk the real ~/.claude/projects on a CI/test machine).

if [[ "${TEST_MODE}" == "1" ]]; then
  say "test-mode: skipping session-search backfill"
else
  say "backfilling existing Claude Code transcripts into the sessions index (may take a few minutes)"
  if "${TOOLS_HOME}/.venv/bin/rekol" session-index --full 2>&1 | sed 's/^/  /'; then
    log_journal "BACKFILLED session index (session-index --full)"
  else
    say "session-search backfill skipped or failed (non-fatal) — run 'rekol session-index --full' later"
  fi
fi

# =============================================================================
# Step 10 — Migrate legacy memory (opt-in via --migrate)
# =============================================================================
# Runs `rekol migrate auto` after seeding to bring any legacy ~/.claude/projects/*/memory/
# content into $REKOL_HOME.  Idempotent — dirs with a retirement pointer are skipped.
# Uses --no-llm because Bedrock creds may not be available at install time; users
# who want LLM classification can rerun `rekol migrate auto --commit` manually.
# Pass --migrate to opt in; new installs skip this path by default.

say "checking for legacy memory to migrate"
if [[ "${TEST_MODE}" == "1" ]]; then
  say "test-mode: skipping rekol migrate"
elif [[ "${DO_MIGRATE}" != "1" ]]; then
  say "skipping legacy migration (pass --migrate to import ~/.claude/projects/*/memory/ content)"
else
  # Use the just-installed unified rekol CLI; idempotent, silent on no-op.
  if "${TOOLS_HOME}/.venv/bin/rekol" migrate auto --commit --no-llm --quiet 2>&1 | sed 's/^/  /'; then
    log_journal "MIGRATED legacy memory (auto)"
  else
    say "rekol migrate auto failed (non-fatal)"
  fi
fi

if [[ "${TEST_MODE}" != "1" ]]; then
  say "run 'rekol init' to index existing Claude Code history and import notes"
fi

# =============================================================================
# Done
# =============================================================================

say ""
say "done."
say "journal: ${JOURNAL}"
say ""
say "next steps:"
say "  1. source ${ZSHRC}   (or open a new terminal)"
say "  2. edit ${RESOLVED_HOME}/always/identity.md   to tell Claude who you are"
say "  3. rekol search \"identity\"   to verify"
