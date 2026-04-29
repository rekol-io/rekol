#!/usr/bin/env bash
# memory-tools installer — idempotent; safe to rerun on an already-bootstrapped machine.
#
# Required environment variable:
#   MEMORY_HOME — path to the memory root directory (Dropbox-backed recommended)
#
# Optional flags:
#   --dry-run       print actions without executing them
#   --no-hook       skip SessionStart hook installation (settings.json only)
#   --no-skill      skip Claude skill installation
#   --no-shellrc    skip ~/.zshrc edits (PATH + MEMORY_HOME export)
#   --test-mode     shorthand for --no-hook --no-skill --no-shellrc (use in tests)
#   --tools-home P  override default ~/.local/share/memory-tools
#   --bin-dir P     override default ~/bin
#
# Per Bash standard: using [[ ]] for conditionals, printf instead of echo -e,
# local for function-scoped vars, SCREAMING_SNAKE_CASE for constants.
# Note: set -euo pipefail used here per explicit installer design requirement.
set -euo pipefail

# Resolve installer's own directory so it works from any working directory
COMPONENT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
readonly COMPONENT_DIR

# Defaults — overridable via flags
TOOLS_HOME_DEFAULT="$HOME/.local/share/memory-tools"
BIN_DIR_DEFAULT="$HOME/bin"
readonly SETTINGS_JSON="$HOME/.claude/settings.json"
readonly SKILL_DIR="$HOME/.claude/skills/memory"
readonly ZSHRC="$HOME/.zshrc"

# Mutable config (set by flag parsing)
DRY_RUN=0
DO_HOOK=1
DO_SKILL=1
DO_SHELLRC=1
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
    --test-mode)  DO_HOOK=0; DO_SKILL=0; DO_SHELLRC=0; shift ;;
    --tools-home) TOOLS_HOME="$2";                      shift 2 ;;
    --bin-dir)    BIN_DIR="$2";                         shift 2 ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

# --- Pre-flight: MEMORY_HOME must be set ---

if [[ -z "${MEMORY_HOME:-}" ]]; then
  printf 'error: MEMORY_HOME is not set. Point it at a Dropbox-backed directory.\n' >&2
  exit 2
fi

# --- Journal setup ---
# Journal records every mutation for post-install auditing.
# The journal lives in MEMORY_HOME/.install-logs/ so it does not pollute the
# root and does not interfere with the is_empty check for template seeding.

TS="$(date +%Y%m%d-%H%M%S)"
readonly TS
JOURNAL_DIR="${MEMORY_HOME}/.install-logs"
JOURNAL="${JOURNAL_DIR}/.install-journal-${TS}.log"

run "mkdir -p '${MEMORY_HOME}'"
run "mkdir -p '${JOURNAL_DIR}'"
if [[ "$DRY_RUN" == "0" ]]; then
  : > "$JOURNAL"
  printf 'memory-tools install %s\n' "$TS" >> "$JOURNAL"
fi

# =============================================================================
# Step 1 — Python venv
# =============================================================================

run "mkdir -p '${TOOLS_HOME}'"

if [[ ! -d "${TOOLS_HOME}/.venv" ]]; then
  say "creating venv at ${TOOLS_HOME}/.venv"
  run "python3 -m venv '${TOOLS_HOME}/.venv'"
  log_journal "CREATED venv ${TOOLS_HOME}/.venv"
fi

say "installing/upgrading memory-tools into venv"
run "'${TOOLS_HOME}/.venv/bin/pip' install -U pip"
run "'${TOOLS_HOME}/.venv/bin/pip' install -U -e '${COMPONENT_DIR}'"

# =============================================================================
# Step 2 — ~/bin shims
# =============================================================================

run "mkdir -p '${BIN_DIR}'"

for cmd in memory-index memory-search memory-capture; do
  local_src="${COMPONENT_DIR}/bin/${cmd}"
  local_dst="${BIN_DIR}/${cmd}"

  # Back up any existing non-symlink file to avoid silently overwriting it
  if [[ -e "$local_dst" ]] && [[ ! -L "$local_dst" ]]; then
    local_backup="${local_dst}.bak-${TS}"
    say "backing up existing ${local_dst} → ${local_backup}"
    run "mv '${local_dst}' '${local_backup}'"
    log_journal "BACKED-UP ${local_dst} -> ${local_backup}"
  fi

  run "ln -sf '${local_src}' '${local_dst}'"
  log_journal "SYMLINK ${local_dst} -> ${local_src}"
done

# =============================================================================
# Step 3 — PATH in ~/.zshrc
# =============================================================================

# Skipped when --no-shellrc or --test-mode is in effect — avoids polluting the
# real ~/.zshrc during acceptance tests and CI runs.
if [[ "$DO_SHELLRC" == "1" ]]; then
  # Only appends when BIN_DIR is not already referenced — avoids duplicate entries
  if ! grep -qs "${BIN_DIR}" "${ZSHRC}" 2>/dev/null; then
    say "adding ${BIN_DIR} to PATH in ${ZSHRC}"
    run "printf '\n# memory-tools\nexport PATH=\"%s:\$PATH\"\n' '${BIN_DIR}' >> '${ZSHRC}'"
    log_journal "APPENDED-PATH ${ZSHRC}"
  fi
fi

# =============================================================================
# Step 4 — MEMORY_HOME export in ~/.zshrc
# =============================================================================

# Skipped when --no-shellrc or --test-mode is in effect.
if [[ "$DO_SHELLRC" == "1" ]]; then
  if ! grep -qs "^export MEMORY_HOME=" "${ZSHRC}" 2>/dev/null; then
    say "adding MEMORY_HOME export to ${ZSHRC}"
    run "printf 'export MEMORY_HOME=\"%s\"\n' '${MEMORY_HOME}' >> '${ZSHRC}'"
    log_journal "APPENDED-MEMORY_HOME ${ZSHRC}"
  fi
fi

# =============================================================================
# Step 5 — Seed $MEMORY_HOME from template/ if empty
# =============================================================================
# Uses -A to include hidden files; the is_empty check covers newly created dirs.

is_empty_memory_home() {
  local dir_path="$1"
  # Returns true (0) when MEMORY_HOME contains no user content.
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

if is_empty_memory_home "${MEMORY_HOME}"; then
  say "${MEMORY_HOME} is empty — seeding from template/"
  run "cp -R '${COMPONENT_DIR}/template/'* '${MEMORY_HOME}/'"
  # Rename every *.example file to its real name (strips the .example suffix)
  run "find '${MEMORY_HOME}' -name '*.example' -print0 \
    | xargs -0 -I {} sh -c 'mv \"\$1\" \"\${1%.example}\"' _ {}"
  log_journal "SEEDED ${MEMORY_HOME} from template"
else
  say "${MEMORY_HOME} is non-empty — skipping template seeding (safe)"
fi

# =============================================================================
# Step 6 — Claude skill install
# =============================================================================

if [[ "$DO_SKILL" == "1" ]]; then
  run "mkdir -p '${SKILL_DIR}'"

  local_skill_src="${COMPONENT_DIR}/skill/memory/skill.md"
  local_skill_dst="${SKILL_DIR}/skill.md"

  # Back up only when content differs — avoids churn on repeated installs
  if [[ -f "$local_skill_dst" ]] && ! cmp -s "${local_skill_src}" "${local_skill_dst}"; then
    local_skill_backup="${local_skill_dst}.bak-${TS}"
    say "backing up existing ${local_skill_dst} → ${local_skill_backup}"
    run "cp '${local_skill_dst}' '${local_skill_backup}'"
    log_journal "BACKED-UP ${local_skill_dst} -> ${local_skill_backup}"
  fi

  run "cp '${local_skill_src}' '${local_skill_dst}'"
  log_journal "INSTALLED skill ${local_skill_dst}"
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
# Step 8 — Dropbox ignore file (best-effort; non-fatal if Dropbox absent)
# =============================================================================
# Prevents the local vector index and write-lock from being synced to Dropbox,
# which would cause conflicts across machines.

if [[ ! -f "${MEMORY_HOME}/.dropboxignore" ]]; then
  say "writing ${MEMORY_HOME}/.dropboxignore to keep .index/ out of Dropbox"
  run "printf '.index/\n.writing.lock\n' > '${MEMORY_HOME}/.dropboxignore'"
  log_journal "CREATED ${MEMORY_HOME}/.dropboxignore"
fi

# =============================================================================
# Step 9 — Build or update the vector index
# =============================================================================
# Run rebuild on first install; update on subsequent runs.

if [[ -f "${MEMORY_HOME}/.index/index.db" ]]; then
  say "existing index found — running memory-index update"
  # Pass MEMORY_TOOLS_HOME so the shim resolves the correct venv when
  # --tools-home overrides the default ~/.local/share/memory-tools path.
  run "MEMORY_TOOLS_HOME='${TOOLS_HOME}' '${BIN_DIR}/memory-index' update"
else
  say "no index found — running memory-index rebuild"
  run "MEMORY_TOOLS_HOME='${TOOLS_HOME}' '${BIN_DIR}/memory-index' rebuild"
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
say "  2. edit ${MEMORY_HOME}/always/identity.md   to tell Claude who you are"
say "  3. memory-search \"identity\"   to verify"
