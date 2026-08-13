#!/usr/bin/env bash
# rekol uninstaller — surgically reverses what install.sh set up.
#
# Idempotent and safe to re-run. Every removal is "if present", so a partial
# install (or a second run) is a clean no-op rather than an error.
#
# SAFETY: your markdown memory is NEVER deleted. The user's content under
# $REKOL_HOME (always/, when/, topics/, knowledge/, *.md, *.config.yaml, the
# local git repo, etc.) is preserved. Only the derived, rebuildable index — the
# local cache outside $REKOL_HOME, plus any legacy in-tree .index/ — may be
# removed, and only with confirmation or --purge-index.
#
# Required environment variable (same resolver as install.sh):
#   REKOL_HOME — path to the memory root (MEMORY_HOME accepted as a fallback).
#
# Flags:
#   --dry-run       print what would change; touch nothing
#   --purge-index   also delete the rebuildable index (local cache + any legacy
#                   in-tree $REKOL_HOME/.index/); off by default
#   --purge-archive also delete the durable transcript archive; off by default.
#                   The archive holds verbatim transcripts and there is no export
#                   in v1, so deletion is IRREVERSIBLE — preserved unless asked.
#                   (Your markdown memory under $REKOL_HOME is never touched.)
#   --yes           non-interactive; assume "yes" to prompts (e.g. CI/scripts)
#   --tools-home P  override default ~/.local/share/rekol (the venv + tools home)
#   --bin-dir P     override default ~/bin (where the rekol shim lives)
#   --help          print usage and exit
#
# Per Bash standard: [[ ]] for conditionals, printf over echo -e, local for
# function-scoped vars, SCREAMING_SNAKE_CASE for constants. set -euo pipefail
# mirrors install.sh — but every removal is guarded so an absent item is fine.
set -euo pipefail

# Resolve this script's own directory so the shim-ownership check can tell
# whether ~/bin/rekol points back into THIS repo (i.e. is ours to remove).
COMPONENT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
readonly COMPONENT_DIR

# Defaults — overridable via flags. Mirror install.sh's resolution so an install
# done with REKOL_TOOLS_HOME/MEMORY_TOOLS_HOME overrides is found and removed.
TOOLS_HOME_DEFAULT="${REKOL_TOOLS_HOME:-${MEMORY_TOOLS_HOME:-$HOME/.local/share/rekol}}"
BIN_DIR_DEFAULT="$HOME/bin"
readonly SETTINGS_JSON="$HOME/.claude/settings.json"
readonly SKILL_BASE="$HOME/.claude/skills"

# Mirror install.sh: clean the rekol exports from the same shell rc install would
# have written, detected from the user's login shell ($SHELL). Defaults to
# ~/.zshrc when $SHELL is unset/unrecognized.
detect_shell_rc() {
  case "$(basename "${SHELL:-zsh}")" in
    bash)
      if [[ "$(uname -s)" == "Darwin" ]]; then printf '%s\n' "$HOME/.bash_profile"
      else printf '%s\n' "$HOME/.bashrc"; fi ;;
    *) printf '%s\n' "$HOME/.zshrc" ;;
  esac
}
SHELL_RC="$(detect_shell_rc)"
readonly SHELL_RC

# Mutable config (set by flag parsing). TOOLS_HOME/BIN_DIR start UNSET so the
# precedence resolver below can distinguish "user passed a flag" from "fall back
# to manifest, then default". A flag sets the value AND its *_FROM_FLAG marker.
DRY_RUN=0
PURGE_INDEX=0
PURGE_ARCHIVE=0
ASSUME_YES=0
TOOLS_HOME=""
BIN_DIR=""
TOOLS_HOME_FROM_FLAG=0
BIN_DIR_FROM_FLAG=0

# --- Helpers ---

say() {
  printf '%s\n' "$*"
}

usage() {
  cat <<'EOF'
rekol uninstall — cleanly remove rekol, preserving your markdown memory.

Usage: ./uninstall.sh [--dry-run] [--purge-index] [--purge-archive] [--yes]
                      [--tools-home PATH] [--bin-dir PATH] [--help]

What it removes:
  - the ~/bin/rekol shim (only if it points back into the rekol repo)
  - ~/.claude/skills/rekol and the ~/.claude/skills/memory shim
  - ~/.local/share/rekol/ (the venv + tools home)
  - rekol hook handlers from ~/.claude/settings.json (SessionStart, PostToolUse,
    SessionEnd, UserPromptSubmit, Stop, StopFailure) and the rekol env keys
    (REKOL_HOME, REKOL_TOOLS_HOME)
  - the rekol PATH + REKOL_HOME (and matching MEMORY_HOME) export lines in your shell rc (~/.zshrc or ~/.bashrc)

What it PRESERVES:
  - all markdown memory under $REKOL_HOME (always/, when/, topics/, knowledge/,
    *.md, *.config.yaml, the local git repo) — your data, never deleted
  - the derived index (local cache outside $REKOL_HOME, plus any legacy in-tree
    .index/) unless you pass --purge-index (or confirm the prompt)
  - the durable transcript archive (verbatim sessions on local disk) unless you
    pass --purge-archive (or confirm the prompt). There is no export in v1, so
    deletion is irreversible — preserved by default, never silently removed.

Path discovery: the venv (tools-home) and shim (bin-dir) are resolved by
precedence — explicit --tools-home/--bin-dir flags, else the install manifest at
$REKOL_HOME/.install-logs/manifest.env, else the built-in defaults. The local
index cache is read from the manifest's INDEX_DIR (or recomputed from the venv
if still present); the durable archive is read from the manifest's ARCHIVE_DIR
(REKOL_ARCHIVE_DIR wins, or the venv's resolver). So a custom install is found
automatically; you only need the flags if the manifest is gone. If a path cannot
be confirmed (no manifest,
default missing), it is reported at the end as a possible leftover instead of
being silently skipped.

Flags:
  --dry-run       print what would change; touch nothing
  --purge-index   also delete the rebuildable index (cache + legacy in-tree)
  --purge-archive also delete the durable transcript archive (irreversible; no
                  export in v1). Your markdown memory is kept either way.
  --yes           non-interactive; assume "yes" to prompts
  --tools-home P  override the venv home (else manifest, else ~/.local/share/rekol)
  --bin-dir P     override the shim dir (else manifest, else ~/bin)
  --help          show this help and exit

Backups: settings.json and the shell rc are copied to timestamped .bak files before
any edit. Your memory markdown is never touched.
EOF
}

# Prints a dry-run notice or executes the given command string. All
# side-effecting operations go through this so --dry-run is reliable.
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    say "DRY-RUN: $*"
  else
    eval "$@"
  fi
}

# Resolves a path to its canonical absolute form WITHOUT requiring it to exist.
# `cd ... pwd -P` resolves symlinks but needs the dir to exist; for a possibly-
# absent path we resolve the deepest existing ancestor and re-append the rest, so
# the overlap check below works even when the archive dir was already removed.
# Falls back to the input unchanged if nothing resolves.
canonical_path() {
  local target="$1"
  [[ -n "$target" ]] || { printf '%s' ""; return 0; }
  if [[ -d "$target" ]]; then
    ( cd -- "$target" 2>/dev/null && pwd -P ) || printf '%s' "$target"
    return 0
  fi
  local parent leaf
  parent="$(dirname -- "$target")"
  leaf="$(basename -- "$target")"
  if [[ -d "$parent" ]]; then
    local resolved_parent
    resolved_parent="$( cd -- "$parent" 2>/dev/null && pwd -P )" || resolved_parent="$parent"
    printf '%s/%s' "$resolved_parent" "$leaf"
  else
    printf '%s' "$target"
  fi
}

# True (0) when two resolved paths OVERLAP: equal, or one contains the other.
# Used to refuse a destructive `rm -rf` of the archive when it would also delete
# (a subtree of) the markdown home. Compares canonical forms with a trailing-slash
# guard so `/a/home` does NOT spuriously "contain" `/a/home-2`.
paths_overlap() {
  local a b
  a="$(canonical_path "$1")"
  b="$(canonical_path "$2")"
  [[ -n "$a" && -n "$b" ]] || return 1
  [[ "$a" == "$b" ]] && return 0
  # a contains b (b is under a), or b contains a (a is under b).
  [[ "$b" == "$a/"* ]] && return 0
  [[ "$a" == "$b/"* ]] && return 0
  return 1
}

# Asks a yes/no question (default no). Returns 0 for yes. Honours --yes
# (auto-yes) and --dry-run (auto-no, since dry-run must change nothing). When
# stdin is not a TTY and --yes was not given, defaults to no (the safe choice
# for a destructive action with no one to answer).
confirm() {
  local prompt="$1" reply
  if [[ "$ASSUME_YES" == "1" ]]; then
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    return 1
  fi
  if [[ ! -t 0 ]]; then
    return 1
  fi
  printf '%s [y/N]: ' "$prompt" >&2
  read -r reply || true
  [[ "$reply" == "y" || "$reply" == "Y" || "$reply" == "yes" ]]
}

# --- Argument parsing ---

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)      DRY_RUN=1;              shift ;;
    --purge-index)  PURGE_INDEX=1;          shift ;;
    --purge-archive) PURGE_ARCHIVE=1;       shift ;;
    --yes|-y)       ASSUME_YES=1;           shift ;;
    --tools-home)   TOOLS_HOME="$2"; TOOLS_HOME_FROM_FLAG=1; shift 2 ;;
    --bin-dir)      BIN_DIR="$2";    BIN_DIR_FROM_FLAG=1;    shift 2 ;;
    --help|-h)      usage; exit 0 ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# Resolve the memory home the same way install.sh does. Unlike install, an
# unset home is NOT fatal: most of the uninstall (shim, skills, venv, settings,
# zshrc) does not need it. We only need it to optionally purge .index/ and to
# decide whether a MEMORY_HOME export line points at the rekol home. When it is
# unset we skip those home-scoped steps and say so.
RESOLVED_HOME="${REKOL_HOME:-${MEMORY_HOME:-}}"
readonly RESOLVED_HOME

TS="$(date +%Y%m%d-%H%M%S)"
readonly TS

REMOVED=()
PRESERVED=()
LEFTOVERS=()
note_removed()   { REMOVED+=("$1"); }
note_preserved() { PRESERVED+=("$1"); }
note_leftover()  { LEFTOVERS+=("$1"); }

# --- Resolve TOOLS_HOME / BIN_DIR by precedence ---
# install.sh accepts custom --tools-home/--bin-dir, so a user who installed with
# custom paths and later runs `uninstall.sh` with no flags must still find the
# right venv + shim. Precedence (highest first):
#   1. explicit --tools-home / --bin-dir flags
#   2. the install manifest ($REKOL_HOME/.install-logs/manifest.env)
#   3. built-in defaults (~/.local/share/rekol, ~/bin)
# The manifest is parsed SAFELY: we read KEY=value lines and whitelist the keys
# (TOOLS_HOME, BIN_DIR, INDEX_DIR, ARCHIVE_DIR) — never `source` it, so a tampered
# manifest cannot run code. We track whether each value came from the manifest so
# the report step can distinguish a confirmed path from a guessed default.
MANIFEST_FILE="${RESOLVED_HOME:+${RESOLVED_HOME}/.install-logs/manifest.env}"
MANIFEST_TOOLS_HOME=""
MANIFEST_BIN_DIR=""
MANIFEST_INDEX_DIR=""
MANIFEST_ARCHIVE_DIR=""
MANIFEST_FOUND=0

# Reads one whitelisted KEY's value from the manifest. Only the exact keys passed
# here are honoured; any other line is ignored. Returns the value on stdout (or
# empty). No `source`, no eval — a line is `KEY=value`, value is everything after
# the first `=` on a line whose key matches exactly.
manifest_value() {
  local key="$1" file="$2" line
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      "${key}="*) printf '%s' "${line#*=}" ;;
    esac
  done < "$file"
}

if [[ -n "$MANIFEST_FILE" && -f "$MANIFEST_FILE" ]]; then
  MANIFEST_FOUND=1
  MANIFEST_TOOLS_HOME="$(manifest_value TOOLS_HOME "$MANIFEST_FILE")"
  MANIFEST_BIN_DIR="$(manifest_value BIN_DIR "$MANIFEST_FILE")"
  MANIFEST_INDEX_DIR="$(manifest_value INDEX_DIR "$MANIFEST_FILE")"
  MANIFEST_ARCHIVE_DIR="$(manifest_value ARCHIVE_DIR "$MANIFEST_FILE")"
fi

# TOOLS_HOME: flag wins; else manifest; else default. Track the source so the
# leftovers report can flag a path we could only *guess* (default, no manifest).
TOOLS_HOME_SOURCE="default"
if [[ "$TOOLS_HOME_FROM_FLAG" == "1" ]]; then
  TOOLS_HOME_SOURCE="flag"
elif [[ -n "$MANIFEST_TOOLS_HOME" ]]; then
  TOOLS_HOME="$MANIFEST_TOOLS_HOME"
  TOOLS_HOME_SOURCE="manifest"
else
  TOOLS_HOME="$TOOLS_HOME_DEFAULT"
fi

BIN_DIR_SOURCE="default"
if [[ "$BIN_DIR_FROM_FLAG" == "1" ]]; then
  BIN_DIR_SOURCE="flag"
elif [[ -n "$MANIFEST_BIN_DIR" ]]; then
  BIN_DIR="$MANIFEST_BIN_DIR"
  BIN_DIR_SOURCE="manifest"
else
  BIN_DIR="$BIN_DIR_DEFAULT"
fi
readonly TOOLS_HOME BIN_DIR TOOLS_HOME_SOURCE BIN_DIR_SOURCE MANIFEST_FOUND MANIFEST_FILE

# --- Resolve the local-only index cache to remove ---
# SECURITY: install relocated the index (index.db + the secrets-bearing
# sessions.db + INDEX.md) OUT of $REKOL_HOME into ${XDG_CACHE_HOME:-~/.cache}/
# rekol/<hash>. Uninstall must remove that cache too, or it lingers forever.
# Resolution (highest precedence first):
#   1. the manifest's recorded INDEX_DIR (exact path install used)
#   2. the still-present venv's resolver (only if the venv exists AND we know
#      REKOL_HOME) — recomputes the same hash the tools use
# Resolve this BEFORE Step 3 removes the venv. If neither source yields a path,
# INDEX_DIR stays empty and Step 6 reports it as a possible leftover.
INDEX_DIR=""
INDEX_DIR_SOURCE="unknown"
if [[ -n "$MANIFEST_INDEX_DIR" ]]; then
  INDEX_DIR="$MANIFEST_INDEX_DIR"
  INDEX_DIR_SOURCE="manifest"
elif [[ -n "$RESOLVED_HOME" && -x "${TOOLS_HOME}/.venv/bin/python" ]]; then
  INDEX_DIR="$(
    REKOL_HOME="${RESOLVED_HOME}" "${TOOLS_HOME}/.venv/bin/python" -c \
      'from rekol.config import resolve_index_dir, resolve_memory_home; from pathlib import Path; import os; print(resolve_index_dir(Path(os.path.expanduser(resolve_memory_home()))))' \
      2>/dev/null || true
  )"
  [[ -n "$INDEX_DIR" ]] && INDEX_DIR_SOURCE="venv"
fi
readonly INDEX_DIR INDEX_DIR_SOURCE

# --- Resolve the durable transcript archive to (optionally) remove ---
# SECURITY/data: the archive is a machine-level copy of verbatim transcripts in a
# durable, non-cache location (${XDG_DATA_HOME:-~/.local/share}/rekol/archive by
# default). It is PRESERVED by default; only --purge-archive (or the confirm
# prompt in Step 6c) removes it. Resolution (highest precedence first):
#   1. $REKOL_ARCHIVE_DIR — explicit override (matches the runtime resolver)
#   2. the manifest's recorded ARCHIVE_DIR (exact path install resolved)
#   3. the still-present venv's resolve_archive_dir(None) — the XDG default
# The resolver is machine-level (NOT hashed per REKOL_HOME), so it takes no home.
# Resolve this BEFORE Step 3 removes the venv. If unresolved, ARCHIVE_DIR stays
# empty and Step 6c reports it as a possible leftover when a purge was requested.
ARCHIVE_DIR=""
ARCHIVE_DIR_SOURCE="unknown"
if [[ -n "${REKOL_ARCHIVE_DIR:-}" ]]; then
  ARCHIVE_DIR="${REKOL_ARCHIVE_DIR}"
  ARCHIVE_DIR_SOURCE="env"
elif [[ -n "$MANIFEST_ARCHIVE_DIR" ]]; then
  ARCHIVE_DIR="$MANIFEST_ARCHIVE_DIR"
  ARCHIVE_DIR_SOURCE="manifest"
elif [[ -x "${TOOLS_HOME}/.venv/bin/python" ]]; then
  ARCHIVE_DIR="$(
    "${TOOLS_HOME}/.venv/bin/python" -c \
      'from rekol.config import resolve_archive_dir; print(resolve_archive_dir(None))' \
      2>/dev/null || true
  )"
  [[ -n "$ARCHIVE_DIR" ]] && ARCHIVE_DIR_SOURCE="venv"
fi
readonly ARCHIVE_DIR ARCHIVE_DIR_SOURCE

say "rekol uninstall — preserving your markdown memory; removing the tooling."
if [[ "$DRY_RUN" == "1" ]]; then
  say "(dry-run: nothing will be changed)"
fi
if [[ "$MANIFEST_FOUND" == "1" ]]; then
  say "using install manifest ${MANIFEST_FILE} to locate the venv + shim"
fi
say "tools home: ${TOOLS_HOME} (${TOOLS_HOME_SOURCE}); bin dir: ${BIN_DIR} (${BIN_DIR_SOURCE})"

# =============================================================================
# Step 1 — remove the ~/bin/rekol shim (only if it is ours)
# =============================================================================
# install.sh Step 2 created ${BIN_DIR}/rekol as a symlink to ${repo}/bin/rekol.
# Remove it ONLY when it resolves into a rekol repo — never clobber an unrelated
# binary a user happens to have named `rekol`. A symlink whose target ends in
# /bin/rekol (this repo's shim path) is ours; we also accept a link pointing at
# THIS repo's shim explicitly.

rekol_shim_dst="${BIN_DIR}/rekol"
our_shim_src="${COMPONENT_DIR}/bin/rekol"
if [[ -L "$rekol_shim_dst" ]]; then
  link_target="$(readlink "$rekol_shim_dst" 2>/dev/null || true)"
  if [[ "$link_target" == "$our_shim_src" || "$link_target" == */bin/rekol ]]; then
    say "removing rekol shim ${rekol_shim_dst} (-> ${link_target})"
    run "rm -f '${rekol_shim_dst}'"
    note_removed "shim ${rekol_shim_dst}"
  else
    say "leaving ${rekol_shim_dst} alone — symlink target ${link_target} is not the rekol shim"
    note_preserved "shim ${rekol_shim_dst} (not ours)"
  fi
elif [[ -e "$rekol_shim_dst" ]]; then
  # A regular file named rekol that is NOT a symlink — not something install.sh
  # created (it only ever symlinks). Leave it; warn so the user can decide.
  say "leaving ${rekol_shim_dst} alone — it is a regular file, not the rekol symlink"
  note_preserved "shim ${rekol_shim_dst} (regular file, not ours)"
elif [[ "$BIN_DIR_SOURCE" == "default" && "$MANIFEST_FOUND" == "0" ]]; then
  # No manifest, no --bin-dir flag, and no shim at the guessed default. A custom
  # install (--bin-dir elsewhere) would leave a shim we cannot see — report it
  # rather than silently claiming a clean removal.
  say "could not confirm the rekol shim — no install manifest and none at the default ${rekol_shim_dst}"
  note_leftover "shim: not found at default ${rekol_shim_dst}; if you installed with a custom --bin-dir, remove that symlink by hand (or re-run with --bin-dir PATH)"
fi

# =============================================================================
# Step 2 — remove the Claude skills (rekol + the memory shim + init + bootstrap)
# =============================================================================
# install.sh Step 6 installed ${SKILL_BASE}/{rekol,memory,rekol-init,rekol-bootstrap}.

for skill_name in rekol memory rekol-init rekol-bootstrap; do
  skill_dir="${SKILL_BASE}/${skill_name}"
  if [[ -d "$skill_dir" ]]; then
    say "removing skill ${skill_dir}"
    run "rm -rf '${skill_dir}'"
    note_removed "skill ${skill_dir}"
  fi
done

# =============================================================================
# Step 3 — remove the tools home (venv + hooks symlinks)
# =============================================================================
# install.sh Steps 1/7B created ${TOOLS_HOME} (the .venv and hooks/). This is
# entirely rekol-owned and rebuildable — safe to remove wholesale. We never
# touch $REKOL_HOME here; TOOLS_HOME is a separate directory.

if [[ -d "$TOOLS_HOME" ]]; then
  say "removing tools home ${TOOLS_HOME} (venv + hook scripts)"
  run "rm -rf '${TOOLS_HOME}'"
  note_removed "tools home ${TOOLS_HOME}"
elif [[ "$TOOLS_HOME_SOURCE" == "default" && "$MANIFEST_FOUND" == "0" ]]; then
  # We had no manifest and no flag, so we could only guess the built-in default —
  # and nothing is there. A custom install (--tools-home elsewhere) would leave a
  # venv we cannot see. Rather than silently "succeed", report it so the user can
  # remove it by hand or re-run with --tools-home PATH.
  say "could not confirm the tools home — no install manifest and the default ${TOOLS_HOME} does not exist"
  note_leftover "tools home: not found at default ${TOOLS_HOME}; if you installed with a custom --tools-home, remove that directory by hand (or re-run with --tools-home PATH)"
fi

# =============================================================================
# Step 4 — strip rekol hooks + env.REKOL_HOME from ~/.claude/settings.json
# =============================================================================
# install.sh Steps 7/7A/7C/7D/7E/7F/7G wired rekol handlers into the
# SessionStart, PostToolUse, SessionEnd, UserPromptSubmit and Stop hook arrays,
# and Step 7.5 set env.REKOL_HOME. Every rekol handler's `command` contains the
# string "rekol" (the index-cat handler references REKOL.md / $REKOL_HOME; the
# others invoke the `rekol` CLI; the auto-reindex handler points at
# .local/share/rekol/hooks/auto-reindex.sh). We drop handlers whose command
# matches "rekol" from each array, prune entries left with zero handlers, prune
# hook arrays left empty, and remove env.REKOL_HOME — leaving every non-rekol
# hook and setting intact. Mirrors install's jq-missing handling.

if [[ -f "$SETTINGS_JSON" ]]; then
  if ! command -v jq >/dev/null 2>&1; then
    say "jq not found; cannot edit ${SETTINGS_JSON} automatically."
    say "  Remove any hook whose command references 'rekol', and the env.REKOL_HOME key, by hand."
    note_preserved "settings.json (jq missing — edit by hand)"
  else
    # Only mutate if there is actually something rekol to remove. Keying on the
    # A rekol hook command, or any env key in the rekol NAMESPACE.
    #
    # Namespace rather than an enumerated list, deliberately: an explicit list has
    # to be updated in two places every time a key is added, and the last time a
    # key was added (REKOL_TOOLS_HOME) exactly one of those places was updated.
    # A detector wider than its remover can never confirm its own claim — it
    # re-reports "removed" on every run while the survivor stays put. Matching by
    # prefix means the detector and the remover cannot drift apart, and any future
    # REKOL_* key is cleaned up without anyone remembering to come back here.
    HAS_REKOL="$(
      jq '
        ([.hooks // {} | .. | objects | .command? // empty] | any(. | test("rekol")))
        or ((.env // {} | keys
             | map(startswith("REKOL_") or . == "MEMORY_HOME" or . == "MEMORY_TOOLS_HOME")
             | any))
      ' "${SETTINGS_JSON}" 2>/dev/null || printf 'false'
    )"

    if [[ "$HAS_REKOL" != "true" ]]; then
      say "no rekol hooks or rekol env keys in ${SETTINGS_JSON} — nothing to strip"
    else
      settings_backup="${SETTINGS_JSON}.bak-${TS}"
      say "backing up ${SETTINGS_JSON} -> ${settings_backup}"
      run "cp '${SETTINGS_JSON}' '${settings_backup}'"
      note_removed "rekol hooks + rekol env keys from ${SETTINGS_JSON} (backup: ${settings_backup})"

      # For each known hook event: drop handlers whose command contains "rekol",
      # prune now-empty entries, and prune the event key if it ends up empty.
      # Then remove env.REKOL_HOME and drop an env block that ends up empty.
      local_tmp="${SETTINGS_JSON}.tmp.$$"
      run "jq '
        def strip_event(ev):
          if (.hooks[ev]? | type) == \"array\" then
            .hooks[ev] = [
              .hooks[ev][]
              | .hooks = [ (.hooks // [])[] | select((.command // \"\") | test(\"rekol\") | not) ]
              | select((.hooks | length) > 0)
            ]
          else . end
          | if (.hooks[ev]? | type) == \"array\" and (.hooks[ev] | length) == 0
            then del(.hooks[ev]) else . end;
        strip_event(\"SessionStart\")
        | strip_event(\"PostToolUse\")
        | strip_event(\"SessionEnd\")
        | strip_event(\"UserPromptSubmit\")
        | strip_event(\"Stop\")
        | strip_event(\"StopFailure\")
        | if has(\"env\") then .env |= with_entries(select(
              (.key | startswith(\"REKOL_\")) or .key == \"MEMORY_HOME\"
              or .key == \"MEMORY_TOOLS_HOME\" | not)) else . end
        | if has(\"env\") and (.env | length) == 0 then del(.env) else . end
        | if has(\"hooks\") and (.hooks | length) == 0 then del(.hooks) else . end
      ' '${SETTINGS_JSON}' > '${local_tmp}' && mv '${local_tmp}' '${SETTINGS_JSON}'"
      say "stripped rekol hooks and rekol env keys from ${SETTINGS_JSON}"
    fi
  fi
fi

# =============================================================================
# Step 5 — remove rekol lines from the shell rc (surgical)
# =============================================================================
# install.sh Step 3 appended a `# rekol` comment line immediately followed by an
# `export PATH="<bindir>:$PATH"` line; Step 4 appended `export REKOL_HOME="..."`.
# A legacy install may also have left `export MEMORY_HOME="<rekol home>"`. We
# remove:
#   - the `# rekol` comment line AND the next line iff it is the rekol PATH export
#   - any `^export REKOL_HOME=` line
#   - a `^export MEMORY_HOME=` line ONLY when its value equals $RESOLVED_HOME
# Everything else (including the user's own lines) is left byte-for-byte intact.
# Implemented in awk for precise line-pair handling; no edit in dry-run mode.

if [[ -f "$SHELL_RC" ]]; then
  # Detect whether there is anything to do, to keep the no-op path side-effect
  # free (no backup churn on a clean .zshrc).
  needs_edit=0
  if grep -qs '^# rekol$' "$SHELL_RC" 2>/dev/null; then needs_edit=1; fi
  if grep -qs '^export REKOL_HOME=' "$SHELL_RC" 2>/dev/null; then needs_edit=1; fi
  if grep -qs "${BIN_DIR}" "$SHELL_RC" 2>/dev/null; then needs_edit=1; fi
  # A MEMORY_HOME export pointing at the resolved rekol home (legacy).
  if [[ -n "$RESOLVED_HOME" ]] \
     && grep -qs "^export MEMORY_HOME=\"\\?${RESOLVED_HOME}\"\\?\$" "$SHELL_RC" 2>/dev/null; then
    needs_edit=1
  fi

  if [[ "$needs_edit" == "0" ]]; then
    say "no rekol lines in ${SHELL_RC} — nothing to remove"
  else
    zshrc_backup="${SHELL_RC}.bak-${TS}"
    say "backing up ${SHELL_RC} -> ${zshrc_backup}"
    run "cp '${SHELL_RC}' '${zshrc_backup}'"

    if [[ "$DRY_RUN" == "1" ]]; then
      say "DRY-RUN: would remove rekol PATH/REKOL_HOME (and matching MEMORY_HOME) lines from ${SHELL_RC}"
    else
      local_tmp="${SHELL_RC}.tmp.$$"
      # awk reads the whole file; BIN_DIR and RESOLVED_HOME passed as vars so we
      # never interpolate them into the program text. The `# rekol` marker
      # suppresses itself and a following rekol-PATH line (the install pair).
      awk -v bindir="${BIN_DIR}" -v memhome="${RESOLVED_HOME}" '
        # Drop the install-time "# rekol" marker; if the very next line is the
        # rekol PATH export, drop it too.
        $0 == "# rekol" { skip_next_if_path = 1; next }
        {
          if (skip_next_if_path == 1) {
            skip_next_if_path = 0
            # Next line right after the marker: drop it if it is a PATH export
            # mentioning our bin dir (the line install.sh paired with # rekol).
            if ($0 ~ /^export PATH=/ && index($0, bindir) > 0) { next }
          }
        }
        # Drop any REKOL_HOME export (current installs).
        /^export REKOL_HOME=/ { next }
        # Drop a stray rekol PATH export even without the marker above it.
        ($0 ~ /^export PATH=/ && index($0, bindir) > 0) { next }
        # Drop a legacy MEMORY_HOME export ONLY when it points at the rekol home.
        {
          if (memhome != "" && $0 ~ /^export MEMORY_HOME=/) {
            val = $0
            sub(/^export MEMORY_HOME=/, "", val)
            gsub(/"/, "", val)
            if (val == memhome) { next }
          }
        }
        { print }
      ' "${SHELL_RC}" > "${local_tmp}" && mv "${local_tmp}" "${SHELL_RC}"
      say "removed rekol lines from ${SHELL_RC}"
    fi
    note_removed "rekol lines in ${SHELL_RC} (backup: ${zshrc_backup})"
  fi
fi

# =============================================================================
# Step 6 — optionally remove the derived vector index (cache + any legacy copy)
# =============================================================================
# The index is machine-specific and rebuildable, not user content. It now lives
# in a local-only cache OUTSIDE $REKOL_HOME (resolved above into INDEX_DIR);
# older installs kept it in-tree at $REKOL_HOME/.index/. Both are removed under
# the same opt-in gate: --purge-index, or explicit confirmation. We NEVER remove
# anything else under $REKOL_HOME. Markdown memory is always preserved.

# Gated removal of one derived-index directory. Honours --purge-index (remove),
# --yes (keep — --yes alone must not purge the index), confirm prompt otherwise.
# $1 = path, $2 = human label for the prompt/report.
purge_index_dir() {
  local target="$1" label="$2"
  if [[ ! -d "$target" ]]; then
    return 0
  fi
  if [[ "$PURGE_INDEX" == "1" ]]; then
    say "removing ${label} ${target} (--purge-index)"
    run "rm -rf '${target}'"
    note_removed "${label} ${target}"
  elif [[ "$ASSUME_YES" == "1" ]]; then
    say "keeping ${label} ${target} (--yes does not purge it; pass --purge-index to remove it)"
    note_preserved "${label} ${target}"
  elif confirm "Also delete the rebuildable ${label} at ${target}? (your markdown is kept either way)"; then
    say "removing ${label} ${target}"
    run "rm -rf '${target}'"
    note_removed "${label} ${target}"
  else
    say "keeping ${label} ${target} (pass --purge-index to remove it)"
    note_preserved "${label} ${target}"
  fi
}

# 6a — the relocated local-only cache (the current home of the index).
if [[ -n "$INDEX_DIR" ]]; then
  purge_index_dir "$INDEX_DIR" "vector index cache"
elif [[ -n "$RESOLVED_HOME" ]]; then
  # We could not resolve the cache path (no manifest INDEX_DIR and no venv to ask
  # — e.g. the venv was already removed by a prior partial uninstall). Report it
  # so the user can clear ${XDG_CACHE_HOME:-~/.cache}/rekol/ by hand rather than
  # silently leaving the cache (and its sessions.db) behind.
  say "could not resolve the local index cache (no INDEX_DIR in manifest, no venv to ask)"
  note_leftover "vector index cache: not resolved; remove the matching dir under \${XDG_CACHE_HOME:-~/.cache}/rekol/ by hand"
else
  say "REKOL_HOME/MEMORY_HOME not set and no manifest INDEX_DIR — skipping index-cache cleanup."
fi

# 6b — a legacy in-tree $REKOL_HOME/.index/ from before the relocation. Removed
# under the same gate; only the .index/ dir, never other $REKOL_HOME content.
if [[ -n "$RESOLVED_HOME" ]]; then
  purge_index_dir "${RESOLVED_HOME}/.index" "legacy in-tree vector index"
fi

# 6c — the durable transcript archive. Preserved by default; verbatim transcripts
# argue for cleanup, but "it's yours / no export in v1" means deletion is
# irreversible — so prompt (or require --purge-archive), never silently nuke.
# Markdown under $REKOL_HOME is never touched here either way.
if [[ -n "$ARCHIVE_DIR" && -d "$ARCHIVE_DIR" ]] \
   && [[ -n "$RESOLVED_HOME" ]] && paths_overlap "$ARCHIVE_DIR" "$RESOLVED_HOME"; then
  # SECURITY: a misconfigured archive dir that equals/contains/is-contained-by the
  # markdown home would make `rm -rf "${ARCHIVE_DIR}"` delete (a subtree of) the
  # user's irreplaceable markdown memory. REFUSE to purge — markdown under
  # $REKOL_HOME must never be deleted — and report it as a leftover so the user can
  # relocate the archive and purge it deliberately.
  say "REFUSING to purge the transcript archive ${ARCHIVE_DIR}: it overlaps your markdown home ${RESOLVED_HOME}."
  say "  Deleting it would risk your markdown memory. Move the archive to a non-overlapping path (REKOL_ARCHIVE_DIR) and re-run with --purge-archive."
  note_preserved "transcript archive ${ARCHIVE_DIR} (overlaps \$REKOL_HOME — not purged to protect markdown)"
  note_leftover "transcript archive: ${ARCHIVE_DIR} overlaps \$REKOL_HOME; relocate it (REKOL_ARCHIVE_DIR) before purging"
elif [[ -n "$ARCHIVE_DIR" && -d "$ARCHIVE_DIR" ]]; then
  if [[ "$PURGE_ARCHIVE" == "1" ]]; then
    say "removing transcript archive ${ARCHIVE_DIR} (--purge-archive)"
    run "rm -rf '${ARCHIVE_DIR}'"
    note_removed "transcript archive ${ARCHIVE_DIR}"
  elif [[ "$ASSUME_YES" == "1" ]]; then
    say "keeping transcript archive ${ARCHIVE_DIR} (--yes does not purge it; pass --purge-archive)"
    note_preserved "transcript archive ${ARCHIVE_DIR}"
  elif confirm "Also delete the durable transcript archive at ${ARCHIVE_DIR}? Irreversible (no export in v1); your markdown is kept either way."; then
    say "removing transcript archive ${ARCHIVE_DIR}"
    run "rm -rf '${ARCHIVE_DIR}'"
    note_removed "transcript archive ${ARCHIVE_DIR}"
  else
    say "keeping transcript archive ${ARCHIVE_DIR} (pass --purge-archive to remove it)"
    note_preserved "transcript archive ${ARCHIVE_DIR}"
  fi
elif [[ "$PURGE_ARCHIVE" == "1" ]]; then
  # A purge was explicitly requested but we could not resolve/find the archive
  # (no env, no manifest ARCHIVE_DIR, no venv to ask, or the dir is already gone).
  # Report it so a custom archive_dir is never silently left behind.
  say "could not resolve the transcript archive to purge (no REKOL_ARCHIVE_DIR, no manifest ARCHIVE_DIR, no venv to ask)"
  note_leftover "transcript archive: not resolved; remove it by hand or set REKOL_ARCHIVE_DIR and re-run with --purge-archive"
fi

# Always record that the markdown memory is preserved.
if [[ -n "$RESOLVED_HOME" ]]; then
  note_preserved "all markdown memory under ${RESOLVED_HOME}"
fi

# =============================================================================
# Summary
# =============================================================================

say ""
say "done."
say ""
if [[ "${#REMOVED[@]}" -gt 0 ]]; then
  say "removed:"
  for item in "${REMOVED[@]}"; do say "  - ${item}"; done
else
  say "removed: nothing (already clean)"
fi
say ""
say "preserved:"
for item in "${PRESERVED[@]}"; do say "  - ${item}"; done

# Report-leftovers: anything we could not confirm we cleaned (no manifest, only
# a guessed default that did not exist). Printed loudly so a custom install never
# silently leaves a venv/shim behind.
if [[ "${#LEFTOVERS[@]}" -gt 0 ]]; then
  say ""
  say "could not confirm (possible leftovers — check manually):"
  for item in "${LEFTOVERS[@]}"; do say "  - ${item}"; done
fi

say ""
say "next steps:"
say "  1. source ${SHELL_RC}   (or open a new terminal) so the removed PATH/exports clear"
say "  2. your memory markdown is untouched at ${RESOLVED_HOME:-<\$REKOL_HOME unset>}"
if [[ "$DRY_RUN" == "1" ]]; then
  say ""
  say "(dry-run: no changes were made)"
fi
