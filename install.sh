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
#   --no-shellrc    skip shell-rc edits (~/.zshrc or ~/.bashrc: PATH + REKOL_HOME)
#   --test-mode     shorthand for --no-hook --no-skill --no-shellrc (use in tests)
#   --tools-home P  override default ~/.local/share/rekol
#   --bin-dir P     override default ~/bin
#   --migrate       opt in to importing legacy ~/.claude/projects/*/memory/ content
#   --no-archive    disable the durable transcript archive (seeds archive_enabled:false)
#   --archive-dir P set the durable archive location (default ~/.local/share/rekol/archive)
#   --help          print this usage block and exit
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

# The interactive shell rc where we add the PATH + REKOL_HOME exports. Detected
# from the user's login shell ($SHELL) so bash users get a working CLI too, not
# just zsh. Defaults to ~/.zshrc (macOS default) when $SHELL is unset/unrecognized.
detect_shell_rc() {
  case "$(basename "${SHELL:-zsh}")" in
    bash)
      # bash login shells read ~/.bash_profile on macOS, ~/.bashrc on most Linux.
      if [[ "$(uname -s)" == "Darwin" ]]; then printf '%s\n' "$HOME/.bash_profile"
      else printf '%s\n' "$HOME/.bashrc"; fi ;;
    *) printf '%s\n' "$HOME/.zshrc" ;;
  esac
}
SHELL_RC="$(detect_shell_rc)"
readonly SHELL_RC

# Mutable config (set by flag parsing)
DRY_RUN=0
DO_HOOK=1
DO_SKILL=1
DO_SHELLRC=1
DO_MIGRATE=0
TEST_MODE=0
# --skip-deps (#78): reuse an already-provisioned venv and skip the pip install
# step entirely. Off by default — production installs always provision deps. Used
# by the bats suite to share one prebuilt venv across tests (the pip install of
# torch dominated runtime), and handy for re-running install to re-wire hooks.
# Also settable via REKOL_INSTALL_SKIP_DEPS=1 (mirrors REKOL_INSTALL_SOURCE_ONLY)
# so a test harness can enable reuse for many invocations without editing each.
SKIP_DEPS="${REKOL_INSTALL_SKIP_DEPS:-0}"
TOOLS_HOME="$TOOLS_HOME_DEFAULT"
BIN_DIR="$BIN_DIR_DEFAULT"
# Durable transcript archive (#8). Default-ON: with no flag we write nothing and
# the code default (archive_enabled:true) applies. --no-archive seeds the off
# switch into the config; --archive-dir relocates it.
DO_ARCHIVE=1
ARCHIVE_DIR=""

# --- Helpers ---

say() {
  printf '%s\n' "$*"
}

usage() {
  cat <<'EOF'
rekol install — idempotent installer; safe to rerun on an already-set-up machine.

Usage: ./install.sh [--dry-run] [--no-hook] [--no-skill] [--no-shellrc]
                    [--test-mode] [--skip-deps] [--tools-home PATH] [--bin-dir PATH]
                    [--migrate] [--no-archive] [--archive-dir PATH] [--help]

Set REKOL_HOME to your memory folder (MEMORY_HOME accepted as a fallback); the
installer prompts for it when neither is set and stdin is a TTY.

Flags:
  --dry-run       print actions without executing them
  --no-hook       skip SessionStart hook installation (settings.json only)
  --no-skill      skip Claude skill installation
  --no-shellrc    skip shell-rc edits (~/.zshrc or ~/.bashrc: PATH + REKOL_HOME)
  --test-mode     shorthand for --no-hook --no-skill --no-shellrc (use in tests)
  --skip-deps     reuse an existing venv and skip the pip install (requires a venv
                  that already has rekol; for fast re-wiring and the test harness)
  --tools-home P  override default ~/.local/share/rekol (the venv + tools home)
  --bin-dir P     override default ~/bin (where the rekol shim lives)
  --migrate       import legacy ~/.claude/projects/*/memory/ content (opt-in)
  --no-archive    disable the durable transcript archive (seeds 'archive_enabled: false')
  --archive-dir P set the durable archive location (default ~/.local/share/rekol/archive)
  --help          show this help and exit

rekol keeps a local copy of your sessions so your memory survives Claude Code
rotating its transcripts — on your disk, never uploaded. Turn it off with
--no-archive (or set 'archive_enabled: false' in rekol.config.yaml).
EOF
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

# Expands a single leading `~` to $HOME (the shell does not expand tilde inside
# variables read from stdin). Anything else is returned unchanged.
expand_leading_tilde() {
  local value="$1"
  case "$value" in
    "~") printf '%s' "$HOME" ;;
    "~/"*) printf '%s/%s' "$HOME" "${value#\~/}" ;;
    *) printf '%s' "$value" ;;
  esac
}

# Lists existing common macOS cloud-sync dirs (best-effort, one per line) so the
# prompt can suggest them. The authoritative detection lives in the Python
# onboarding.detect module (`rekol init`); this is a deliberately thin shell
# subset that must never fail the install if nothing matches.
detected_cloud_sync_dirs() {
  local candidate glob
  for candidate in \
    "$HOME/Library/CloudStorage/Dropbox" \
    "$HOME/Dropbox" \
    "$HOME/Library/Mobile Documents/com~apple~CloudDocs"; do
    if [[ -d "$candidate" ]]; then printf '%s\n' "$candidate"; fi
  done
  # GoogleDrive-* / OneDrive-* mount under CloudStorage with an account suffix;
  # glob them so a present account is suggested without hard-coding the email.
  for glob in \
    "$HOME/Library/CloudStorage/GoogleDrive-"* \
    "$HOME/Library/CloudStorage/OneDrive-"*; do
    if [[ -d "$glob" ]]; then printf '%s\n' "$glob"; fi
  done
  # Always succeed: a no-match must not make `set -e` abort the caller's
  # `suggestions="$(detected_cloud_sync_dirs)"` assignment.
  return 0
}

# Interactively resolves a memory-home path from the user, defaulting to
# DEFAULT_HOME on empty input and expanding a leading tilde. Suggestions (any
# detected cloud-sync dirs) and the prompt go to stderr so the resolved path is
# the only thing on stdout — callers capture it with $(...). The `read` is
# guarded with `|| true` so an EOF under `set -e`/`pipefail` does not abort the
# install before the value is resolved.
prompt_for_memory_home() {
  local default_home="$1"
  local suggestions reply
  suggestions="$(detected_cloud_sync_dirs)"
  if [[ -n "$suggestions" ]]; then
    {
      printf 'Detected cloud-sync folders you could keep memory in:\n'
      # Per-line, quoted: a detected path may contain spaces (e.g.
      # "~/Library/Mobile Documents/...") which unquoted word-splitting would mangle.
      while IFS= read -r _sugg; do printf '  %s\n' "$_sugg"; done <<<"$suggestions"
      printf '(or any local folder; the index lives in a local cache outside it, so syncing your memory never syncs the index)\n'
    } >&2
  fi
  printf 'Memory folder — type a path, or press Enter to use the default [%s]: ' "$default_home" >&2
  read -r reply || true
  if [[ -z "$reply" ]]; then
    reply="$default_home"
  fi
  expand_leading_tilde "$reply"
}

# Sourcing hook: when REKOL_INSTALL_SOURCE_ONLY=1, define the helpers above and
# return before any installation runs, so tests can exercise the prompt logic in
# isolation. Harmless in normal use (the variable is unset).
if [[ "${REKOL_INSTALL_SOURCE_ONLY:-0}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

# --- Argument parsing ---

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)    DRY_RUN=1;                            shift ;;
    --no-hook)    DO_HOOK=0;                            shift ;;
    --no-skill)   DO_SKILL=0;                           shift ;;
    --no-shellrc) DO_SHELLRC=0;                         shift ;;
    --migrate)    DO_MIGRATE=1;                         shift ;;
    --test-mode)  DO_HOOK=0; DO_SKILL=0; DO_SHELLRC=0; TEST_MODE=1; shift ;;
    --skip-deps)  SKIP_DEPS=1;                          shift ;;
    --tools-home) TOOLS_HOME="$2";                      shift 2 ;;
    --bin-dir)    BIN_DIR="$2";                         shift 2 ;;
    --no-archive) DO_ARCHIVE=0;                         shift ;;
    --archive-dir) ARCHIVE_DIR="$2";                    shift 2 ;;
    --help|-h)    usage; exit 0 ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

# --- Pre-flight: REKOL_HOME (or MEMORY_HOME fallback) must be set ---
# When neither is set we prompt for a folder IF stdin is a TTY (interactive
# install), defaulting to $HOME/rekol-memory. When stdin is NOT a TTY
# (CI/piped/non-interactive) we keep the hard `exit 2` — there is no one to
# answer the prompt, so failing loudly is correct.

if [[ -z "${REKOL_HOME:-}" && -z "${MEMORY_HOME:-}" ]]; then
  if [[ -t 0 ]]; then
    REKOL_HOME="$(prompt_for_memory_home "$HOME/rekol-memory")"
  else
    printf 'error: neither REKOL_HOME nor MEMORY_HOME is set. Point REKOL_HOME at any directory (sync it via Dropbox/iCloud/git/Syncthing or keep it local) (MEMORY_HOME is accepted as a fallback).\n' >&2
    exit 2
  fi
fi

# REKOL_HOME is the primary data-directory variable; MEMORY_HOME is kept as a
# fallback so existing installs (which export MEMORY_HOME) keep working. All
# mkdir/journal/seed/index logic below operates on this resolved path.
RESOLVED_HOME="${REKOL_HOME:-$MEMORY_HOME}"
readonly RESOLVED_HOME

# Export the resolved home so EVERY `rekol`/python subprocess below inherits it.
# When the user answers the prompt instead of pre-exporting REKOL_HOME, the
# assignment above is a non-exported shell var, so an un-prefixed subprocess
# (index rebuild / session-index / archive / migrate) would hit
# `load_config()` with no REKOL_HOME and crash the install on its last steps
# (launch-blocker found in clean-account testing). One export fixes the whole
# class — no per-call prefix to forget.
export REKOL_HOME="${RESOLVED_HOME}"

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

# Manifest path is stable and computable now; it is WRITTEN after Step 1 below,
# once the venv exists and the local-only INDEX_DIR can be resolved from the same
# Python the tools use (single source of truth — see the manifest block there).
MANIFEST="${JOURNAL_DIR}/manifest.env"

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
  # A SUITABLE interpreter is Python >=3.11 AND has sqlite3.enable_load_extension
  # (so sqlite-vec's vec0 KNN can load instead of silently degrading to a numpy
  # scan). We probe candidates in preference order and HARD-FAIL with guidance if
  # none qualify — a clear early error beats a too-old python failing later at
  # pip, or a no-extension python silently degrading search.
  _py_suitable() {
    [[ -n "$1" && -x "$1" ]] && "$1" -c 'import sys,sqlite3;sys.exit(0 if sys.version_info>=(3,11) and hasattr(sqlite3.connect(":memory:"),"enable_load_extension") else 1)' >/dev/null 2>&1
  }
  PY3=""
  # 1. uv-managed Python (python-build-standalone — always has extensions).
  if [[ -z "$PY3" ]] && command -v uv >/dev/null 2>&1; then
    uv_py="$(uv python find 2>/dev/null || true)"
    _py_suitable "$uv_py" && PY3="$uv_py"
  fi
  # 2. Versioned / Homebrew pythons (incl. keg-only python@3.12 / @3.11, which the
  #    default `python@3` opt-prefix misses), then whatever python3 is on PATH.
  if [[ -z "$PY3" ]]; then
    for candidate in \
        "$(command -v python3.12 2>/dev/null || true)" \
        "$(command -v python3.11 2>/dev/null || true)" \
        /opt/homebrew/opt/python@3.12/bin/python3.12 \
        /opt/homebrew/opt/python@3.11/bin/python3.11 \
        /usr/local/opt/python@3.12/bin/python3.12 \
        /usr/local/opt/python@3.11/bin/python3.11 \
        /opt/homebrew/bin/python3 \
        /usr/local/opt/python@3/bin/python3 \
        "$(command -v python3 2>/dev/null || true)"; do
      if _py_suitable "$candidate"; then
        PY3="$candidate"
        break
      fi
    done
  fi
  if [[ -z "$PY3" ]]; then
    {
      printf 'error: no suitable Python found. rekol needs Python >=3.11 whose sqlite3 has\n'
      printf '       enable_load_extension (required for sqlite-vec vector search). macOS\n'
      printf '       system python and the python.org installer ship this disabled. Install one:\n'
      printf '         brew install uv && uv python install 3.12   # install.sh picks it up automatically\n'
      printf '       or: brew install python\n'
    } >&2
    exit 2
  fi
  say "creating venv at ${TOOLS_HOME}/.venv using ${PY3}"
  run "'${PY3}' -m venv '${TOOLS_HOME}/.venv'"
  log_journal "CREATED venv ${TOOLS_HOME}/.venv interpreter=${PY3}"
fi

if [[ "$SKIP_DEPS" == "1" ]]; then
  # #78: reuse a pre-provisioned venv (test harness / fast re-wiring). The pip
  # install of rekol's deps — dominated by torch — is the slow step; skip it and
  # trust the existing venv. Hard-fail if rekol isn't actually there, so a missing
  # venv surfaces as a clear error rather than a later cryptic "rekol: not found".
  if [[ ! -x "${TOOLS_HOME}/.venv/bin/rekol" ]]; then
    printf 'error: --skip-deps needs an existing venv with rekol at %s\n' \
      "${TOOLS_HOME}/.venv" >&2
    exit 2
  fi
  say "skipping dependency install (--skip-deps); reusing venv at ${TOOLS_HOME}/.venv"
else
  say "installing/upgrading rekol into venv"
  run "'${TOOLS_HOME}/.venv/bin/pip' install -U pip"
  run "'${TOOLS_HOME}/.venv/bin/pip' install -U -e '${COMPONENT_DIR}'"
fi

# --- Resolve the local-only index/cache dir (single source of truth) ---
# SECURITY: the index (index.db + the secrets-bearing sessions.db + INDEX.md)
# lives OUTSIDE $REKOL_HOME, under ${XDG_CACHE_HOME:-~/.cache}/rekol/<hash>, so a
# synced memory folder never carries derived/secret state. We ask the freshly
# installed venv for the path rather than re-deriving the hash in shell, so the
# manifest, the Step 9 gate, and the legacy-migration cleanup all agree exactly
# with what `rekol` uses at runtime. In dry-run the venv may not exist; fall back
# to a best-effort note so the message stays meaningful.
INDEX_DIR=""
if [[ "$DRY_RUN" == "1" ]]; then
  say "DRY-RUN: resolve local index dir via '${TOOLS_HOME}/.venv/bin/python'"
else
  INDEX_DIR="$(
    REKOL_HOME="${RESOLVED_HOME}" "${TOOLS_HOME}/.venv/bin/python" -c \
      'from rekol.config import resolve_index_dir, resolve_memory_home; from pathlib import Path; import os; print(resolve_index_dir(Path(os.path.expanduser(resolve_memory_home()))))' \
      2>/dev/null || true
  )"
  if [[ -z "$INDEX_DIR" ]]; then
    say "ERROR: could not resolve the local index dir from the venv" >&2
    exit 1
  fi
  say "local index/cache dir: ${INDEX_DIR}"
fi
readonly INDEX_DIR

# --- Resolve the durable archive dir for the manifest (mirrors INDEX_DIR) ---
# Precedence matches the runtime resolver: REKOL_ARCHIVE_DIR > --archive-dir flag >
# the venv's resolve_archive_dir default. Recorded so uninstall can find/remove it
# without re-deriving the XDG default. (Note: the archive is machine-level, NOT
# hashed per REKOL_HOME — resolve_archive_dir takes the raw config value only.)
# Skip the venv call under --dry-run (the venv may not exist), like INDEX_DIR.
ARCHIVE_DIR_RESOLVED=""
if [[ -n "${REKOL_ARCHIVE_DIR:-}" ]]; then
  ARCHIVE_DIR_RESOLVED="${REKOL_ARCHIVE_DIR}"
elif [[ -n "$ARCHIVE_DIR" ]]; then
  ARCHIVE_DIR_RESOLVED="${ARCHIVE_DIR}"
elif [[ "$DRY_RUN" == "1" ]]; then
  say "DRY-RUN: resolve archive dir via '${TOOLS_HOME}/.venv/bin/python'"
else
  ARCHIVE_DIR_RESOLVED="$(
    "${TOOLS_HOME}/.venv/bin/python" -c \
      'from rekol.config import resolve_archive_dir; print(resolve_archive_dir(None))' \
      2>/dev/null || true
  )"
fi
readonly ARCHIVE_DIR_RESOLVED

# --- Install manifest ---
# Records the resolved install parameters so uninstall.sh can be deterministic:
# when a user installs with custom --tools-home/--bin-dir and later runs
# uninstall with no flags, the uninstaller reads this manifest to find the exact
# venv + shim (and the local index cache) it must remove — instead of guessing
# built-in defaults and silently leaving the custom paths behind.
#
# Location: a STABLE path under $REKOL_HOME that uninstall can find knowing only
# REKOL_HOME. Reuses .install-logs/.  Format: simple KEY=value lines, overwritten
# on every (re)install (idempotent). Uninstall reads it by whitelisting keys — it
# never sources this file. INDEX_DIR is recorded so uninstall can remove the
# local cache without re-deriving the hash; ARCHIVE_DIR is recorded so uninstall
# can find the durable transcript archive without re-deriving the XDG default.
if [[ "$DRY_RUN" == "1" ]]; then
  say "DRY-RUN: write install manifest ${MANIFEST}"
else
  {
    printf '# rekol install manifest — written by install.sh; read by uninstall.sh.\n'
    printf '# Whitelisted KEY=value lines only; never sourced. Safe to delete.\n'
    printf 'TOOLS_HOME=%s\n' "${TOOLS_HOME}"
    printf 'BIN_DIR=%s\n' "${BIN_DIR}"
    printf 'REKOL_HOME=%s\n' "${RESOLVED_HOME}"
    printf 'SHIM=%s\n' "${BIN_DIR}/rekol"
    printf 'INDEX_DIR=%s\n' "${INDEX_DIR}"
    printf 'ARCHIVE_DIR=%s\n' "${ARCHIVE_DIR_RESOLVED}"
    printf 'INSTALLED_AT=%s\n' "${TS}"
  } > "${MANIFEST}"
  log_journal "WROTE manifest ${MANIFEST}"
  say "wrote install manifest ${MANIFEST}"
fi

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
# Step 3 — PATH in the shell rc
# =============================================================================

# Skipped when --no-shellrc or --test-mode is in effect — avoids polluting the
# real shell rc during acceptance tests and CI runs.
if [[ "$DO_SHELLRC" == "1" ]]; then
  # Only appends when BIN_DIR is not already referenced — avoids duplicate entries
  if ! grep -qs "${BIN_DIR}" "${SHELL_RC}" 2>/dev/null; then
    say "adding ${BIN_DIR} to PATH in ${SHELL_RC}"
    run "printf '\n# rekol\nexport PATH=\"%s:\$PATH\"\n' '${BIN_DIR}' >> '${SHELL_RC}'"
    log_journal "APPENDED-PATH ${SHELL_RC}"
  fi
fi

# =============================================================================
# Step 4 — REKOL_HOME export in the shell rc
# =============================================================================

# Skipped when --no-shellrc or --test-mode is in effect.
# Exports REKOL_HOME (the primary data-directory variable); guarded so it is not
# re-added on reruns.
if [[ "$DO_SHELLRC" == "1" ]]; then
  if ! grep -qs "^export REKOL_HOME=" "${SHELL_RC}" 2>/dev/null; then
    say "adding REKOL_HOME export to ${SHELL_RC}"
    # #83: write a default-if-unset guard, NOT a hardcoded value. A fresh shell has
    # REKOL_HOME unset → still resolves to the installed path (no change for the
    # common single-store user). But an inherited REKOL_HOME (automation/CI/tests
    # redirecting to a throwaway store, or settings.json relocating it) now SURVIVES
    # re-sourcing the rc instead of being silently clobbered back to the baked path.
    # The \$ is escaped so the literal ${REKOL_HOME:-...} lands in the rc — install.sh
    # itself has REKOL_HOME set, so an unescaped form would expand here at write time.
    run "printf 'export REKOL_HOME=\"\${REKOL_HOME:-%s}\"\n' '${RESOLVED_HOME}' >> '${SHELL_RC}'"
    log_journal "APPENDED-REKOL_HOME ${SHELL_RC}"
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
# Step 5.5 — Seed archive config keys when overridden by flags
# =============================================================================
# Default-ON: with no flag we write nothing (the code default is
# archive_enabled:true). --no-archive seeds the off switch; --archive-dir seeds
# the relocation. Both writes are idempotent — appended only if the key is absent.
#
# Resolve the config file to seed into, mirroring the Step 8.5 CONFIG_YAML block:
# prefer rekol.config.yaml; fall back to a legacy memory.config.yaml ONLY if one
# already exists (an older root). On a fresh home (neither present) we seed the
# CURRENT name and never create the legacy file. Step 8.5 runs much later in the
# script, so we must resolve our own copy here rather than reuse its CONFIG_YAML.
CONFIG_YAML_SEED="${RESOLVED_HOME}/rekol.config.yaml"
if [[ ! -f "${CONFIG_YAML_SEED}" && -f "${RESOLVED_HOME}/memory.config.yaml" ]]; then
  CONFIG_YAML_SEED="${RESOLVED_HOME}/memory.config.yaml"
fi
if [[ "$DO_ARCHIVE" == "0" ]]; then
  if ! grep -qs '^archive_enabled:' "${CONFIG_YAML_SEED}" 2>/dev/null; then
    run "printf 'archive_enabled: false\n' >> '${CONFIG_YAML_SEED}'"
    log_journal "SEEDED archive_enabled:false in ${CONFIG_YAML_SEED}"
  fi
fi
if [[ -n "$ARCHIVE_DIR" ]]; then
  if ! grep -qs '^archive_dir:' "${CONFIG_YAML_SEED}" 2>/dev/null; then
    run "printf 'archive_dir: %s\n' '${ARCHIVE_DIR}' >> '${CONFIG_YAML_SEED}'"
    log_journal "SEEDED archive_dir:${ARCHIVE_DIR} in ${CONFIG_YAML_SEED}"
  fi
fi

# =============================================================================
# Step 6 — Claude skill install
# =============================================================================

if [[ "$DO_SKILL" == "1" ]]; then
  # Install the canonical `rekol` skill, the `memory` back-compat shim, the
  # `rekol-init` assistant-led onboarding interview (#59), and the
  # `rekol-bootstrap` cold-start routine (#41). Claude Code derives the /<name>
  # trigger from the directory name and has no aliases field, so the shim
  # directory is what keeps `/memory` working, `rekol-init` gets its own
  # /rekol-init trigger, and `rekol-bootstrap` gets /rekol-bootstrap.
  for skill_name in rekol memory rekol-init rekol-bootstrap; do
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
# Step 7A — strip the deprecated SessionStart ingest-nudge handler
# =============================================================================
# An earlier rekol version wired a `rekol _hook session-start-nudge` handler into
# the SessionStart block. Onboarding is now pull-only (the user asks the agent to
# set up memory), so that auto-nudge was removed. The code revert leaves the
# handler already wired on existing machines, so (re-)install must actively strip
# it: drop any SessionStart handler whose command contains `session-start-nudge`,
# leave every other handler intact, and prune entries left with no handlers.
# Idempotent — a no-op when no nudge handler is present.

if [[ "$DO_HOOK" == "1" ]] && command -v jq >/dev/null 2>&1; then
  HAS_SS_NUDGE="$(
    jq '[.hooks.SessionStart[]?.hooks[]?.command] | any(. | contains("session-start-nudge"))' \
      "${SETTINGS_JSON}" 2>/dev/null || printf 'false'
  )"
  if [[ "$HAS_SS_NUDGE" != "true" ]]; then
    say "no deprecated SessionStart ingest-nudge handler to remove — no-op"
  else
    local_settings_ssnudge_backup="${SETTINGS_JSON}.bak-ssnudge-${TS}"
    run "cp '${SETTINGS_JSON}' '${local_settings_ssnudge_backup}'"
    log_journal "BACKED-UP ${SETTINGS_JSON} -> ${local_settings_ssnudge_backup}"
    local_tmp="${SETTINGS_JSON}.tmp.$$"
    run "jq '.hooks.SessionStart = [
        .hooks.SessionStart[]?
        | .hooks = [.hooks[]? | select((.command // \"\") | contains(\"session-start-nudge\") | not)]
        | select((.hooks | length) > 0)
      ]' '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
    log_journal "REMOVED deprecated SessionStart ingest-nudge handler from ${SETTINGS_JSON}"
    say "removed deprecated SessionStart ingest-nudge handler from ${SETTINGS_JSON}"
  fi
fi

# =============================================================================
# Step 7.5 — REKOL_HOME into ~/.claude/settings.json env block
# =============================================================================
# Claude Code sessions do NOT source your shell rc, so the shell-level REKOL_HOME
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
# with matcher "Write|Edit|MultiEdit".  Every time the agent edits a file under
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
    # State of the existing PostToolUse wiring for our command:
    #   absent  — command not wired at all                    → append the snippet
    #   stale   — command wired but matcher lacks MultiEdit    → upgrade matcher
    #   current — command wired with a MultiEdit matcher       → no-op
    # The old check keyed ONLY on the command, so a re-install was a no-op for any
    # user who already had the pre-MultiEdit "Write|Edit" matcher — the MultiEdit
    # coverage fix never reached them. Detect the stale matcher and repair it.
    PTU_STATE="$(
      jq -r --slurpfile snip "${SNIPPET_PTU}" '
        (.hooks.PostToolUse // []) as $cur
        | ($snip[0].hooks.PostToolUse[0].hooks[0].command) as $cmd
        | [ $cur[] | select(.hooks // [] | any(.command == $cmd)) ] as $matching
        | if ($matching | length) == 0 then "absent"
          elif ($matching | any((.matcher // "") | contains("MultiEdit"))) then "current"
          else "stale" end
      ' "${SETTINGS_JSON}" 2>/dev/null || printf 'absent'
    )"

    if [[ "$PTU_STATE" == "current" ]]; then
      say "PostToolUse auto-reindex hook already present — no-op"
    else
      # Independent backup for this step (Step 7's backup predates the env
      # mutation in Step 7.5 and would not reflect post-7.5 state)
      local_settings_ptu_backup="${SETTINGS_JSON}.bak-ptu-${TS}"
      run "cp '${SETTINGS_JSON}' '${local_settings_ptu_backup}'"
      log_journal "BACKED-UP ${SETTINGS_JSON} -> ${local_settings_ptu_backup}"

      local_tmp="${SETTINGS_JSON}.tmp.$$"
      if [[ "$PTU_STATE" == "stale" ]]; then
        say "PostToolUse auto-reindex matcher is outdated — upgrading to include MultiEdit"
        run "jq --slurpfile snip '${SNIPPET_PTU}' \
          '(\$snip[0].hooks.PostToolUse[0].hooks[0].command) as \$cmd \
           | (\$snip[0].hooks.PostToolUse[0].matcher) as \$want \
           | .hooks.PostToolUse |= map(if (.hooks // [] | any(.command == \$cmd)) then .matcher = \$want else . end)' \
          '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
        log_journal "UPGRADED PostToolUse auto-reindex matcher in ${SETTINGS_JSON}"
      else
        run "jq --slurpfile snip '${SNIPPET_PTU}' \
          '.hooks.PostToolUse = ((.hooks.PostToolUse // []) + \$snip[0].hooks.PostToolUse)' \
          '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
        log_journal "MERGED PostToolUse auto-reindex hook into ${SETTINGS_JSON}"
      fi
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
# Step 8 — (removed) sync-ignore file for the local vector index
# =============================================================================
# Previously wrote $REKOL_HOME/.dropboxignore to keep the in-tree .index/ out of
# Dropbox sync. The index now lives in a machine-local cache OUTSIDE $REKOL_HOME
# (see the INDEX_DIR resolution after Step 1), so nothing derived sits in the
# synced tree and no per-tool ignore file is needed. Intentionally a no-op.

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
    # No .index/ entry: the index now lives in a local cache outside $REKOL_HOME,
    # so there is no derived state in the tree to exclude from version control.
    say "writing ${RESOLVED_HOME}/.gitignore"
    run "printf '.writing.lock\n.install-logs/\n' > '${RESOLVED_HOME}/.gitignore'"
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
# Step 9 — Build or update the vector index (in the local-only cache)
# =============================================================================
# Run rebuild on first install; update on subsequent runs. The index DB now
# lives at ${INDEX_DIR}/index.db (outside $REKOL_HOME), so the gate checks the
# cache, not the old in-tree .index/. When an old in-tree .index/ exists but the
# cache does not, this falls through to a rebuild — which populates the cache —
# and Step 9.6 then removes the stale (secrets-bearing) in-tree .index/.

if [[ "$DRY_RUN" == "1" ]]; then
  say "DRY-RUN: '${TOOLS_HOME}/.venv/bin/rekol' index update/rebuild (index dir: ${INDEX_DIR:-<resolved at runtime>})"
elif [[ -f "${INDEX_DIR}/index.db" ]]; then
  # Update on a current index; rebuild if it is a legacy (pre-timestamp) schema.
  # `rekol index update` exits non-zero and instructs a rebuild on an outdated
  # schema — that must NOT abort the install (it would skip the Step 9.5 session
  # backfill), so we catch it and rebuild. Invoke the venv entrypoint directly so
  # the correct venv is used when --tools-home overrides the default.
  say "existing index found at ${INDEX_DIR} — running rekol index update (rebuild if schema is outdated)"
  if ! "${TOOLS_HOME}/.venv/bin/rekol" index update; then
    say "index update did not apply (legacy schema) — running rekol index rebuild"
    "${TOOLS_HOME}/.venv/bin/rekol" index rebuild
  fi
else
  say "no index in cache — running rekol index rebuild (writes to ${INDEX_DIR})"
  "${TOOLS_HOME}/.venv/bin/rekol" index rebuild
fi

# =============================================================================
# Step 9.6 — Migrate away from an old in-tree $REKOL_HOME/.index/
# =============================================================================
# SECURITY: older installs kept the index (including sessions.db, which records
# every prompt/assistant turn verbatim — and thus any pasted secrets) inside
# $REKOL_HOME. If that folder is synced (Dropbox/iCloud/Drive/OneDrive/Syncthing/
# git), those secrets leak. Step 9 above has just (re)built the index in the
# local-only cache, so the in-tree copy is now redundant AND dangerous — delete
# it. We remove ONLY the .index/ directory and never touch any markdown or other
# content under $REKOL_HOME.

OLD_INTREE_INDEX="${RESOLVED_HOME}/.index"
if [[ -d "${OLD_INTREE_INDEX}" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    say "DRY-RUN: rm -rf ${OLD_INTREE_INDEX} (legacy in-tree index; rebuilt into ${INDEX_DIR:-the cache})"
  else
    say "removing legacy in-tree index ${OLD_INTREE_INDEX} (rebuilt into ${INDEX_DIR}; its sessions.db must not stay in a synced folder)"
    rm -rf "${OLD_INTREE_INDEX}"
    log_journal "MIGRATED-INDEX removed legacy ${OLD_INTREE_INDEX}; rebuilt into ${INDEX_DIR}"
  fi
fi

# Also drop a now-stale $REKOL_HOME/.dropboxignore that older installs wrote to
# keep the in-tree .index/ out of Dropbox. With the index relocated there is
# nothing left for it to protect; leaving it would be confusing. Only remove the
# exact two-line file we used to write, so a user-authored .dropboxignore is
# never clobbered.
STALE_DBIGNORE="${RESOLVED_HOME}/.dropboxignore"
if [[ -f "${STALE_DBIGNORE}" ]] \
   && [[ "$(printf '.index/\n.writing.lock\n')" == "$(cat "${STALE_DBIGNORE}")" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    say "DRY-RUN: rm ${STALE_DBIGNORE} (stale: index no longer in-tree)"
  else
    say "removing stale ${STALE_DBIGNORE} (the index no longer lives in ${RESOLVED_HOME})"
    rm -f "${STALE_DBIGNORE}"
    log_journal "REMOVED stale ${STALE_DBIGNORE}"
  fi
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
elif [[ "${DRY_RUN}" == "1" ]]; then
  # session-index --full does archive-then-ingest, so it writes to both the index
  # and the durable archive — it must be skipped under --dry-run (it isn't routed
  # through run(), and the venv may not exist on a dry run).
  say "DRY-RUN: '${TOOLS_HOME}/.venv/bin/rekol' session-index --full (backfill existing transcripts into the sessions index)"
else
  say "backfilling existing Claude Code transcripts into the sessions index (may take a few minutes)"
  if "${TOOLS_HOME}/.venv/bin/rekol" session-index --full 2>&1 | sed 's/^/  /'; then
    log_journal "BACKFILLED session index (session-index --full)"
  else
    say "session-search backfill skipped or failed (non-fatal) — run 'rekol session-index --full' later"
  fi
fi

# =============================================================================
# Step 9.7 — Backfill the durable transcript archive from the freshly built index
# =============================================================================
# With the index seeded (Step 9.5), reconstruct the durable archive from it so a
# fresh install has a complete, rebuild-safe copy of every transcript on disk.
# Skipped in --test-mode (would walk the real ~/.claude/projects on CI) and when
# --no-archive disables the archive. Non-fatal (set -euo pipefail is in effect):
# an unwritable archive must never abort an otherwise-good install. Also skipped
# under --dry-run, where the venv may not exist.

if [[ "${TEST_MODE}" != "1" && "${DO_ARCHIVE}" == "1" && "${DRY_RUN}" != "1" ]]; then
  say "building the durable transcript archive (rekol archive --from-index)"
  if "${TOOLS_HOME}/.venv/bin/rekol" archive --from-index 2>&1 | sed 's/^/  /'; then
    log_journal "BACKFILLED archive (archive --from-index)"
  else
    say "archive backfill skipped or failed (non-fatal) — run 'rekol archive --from-index' later"
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
# ONE call-to-action. The assistant-led "/rekol init" flow owns identity,
# indexing, verification, and explaining what rekol does — in context. The
# terminal's only job is to get the user there, with the two genuine
# fresh-start prerequisites stated. Everything else lives in the assistant
# flow / README (issue: post-install copy drifted into a manual checklist).
say ""
say "✓ rekol installed. Your memory lives at ${RESOLVED_HOME} — local, never uploaded."
# Default-ON honesty: one plain line disclosing the local transcript archive
# (verbatim sessions on disk). The opt-out instruction lives in the README.
if [[ "${DO_ARCHIVE}" == "1" ]]; then
  say "  (rekol keeps a local copy of your sessions so memory survives — on your disk, never uploaded; opt out in the README.)"
fi
say ""
say "Open a NEW terminal and start a NEW Claude Code session, then say:"
say ""
say "    \"set up my rekol memory\""
say ""
say "Claude takes it from there."
say ""
# Why both must be fresh — stated so it's not a mysterious instruction:
say "(New terminal: re-sources your shell so \`rekol\` is on PATH. New Claude"
say " session: loads the SessionStart hook + rekol skill the install just wired —"
say " an already-open session won't have them, so the phrase won't work there.)"
