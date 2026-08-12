"""Drift detection: is what I have actually installed? (#27, no network).

The motivating finding: a machine ran a *current* checkout while its recorded
install was 65 days and three minor versions old, and was missing three hooks
shipped in that window. A version check would have said "you're up to date" — the
code WAS current. Nothing compared the code against the wiring.

So this module answers two questions, both offline:

1. **Version drift** — does the version recorded at install time match the code
   running now? Answered from the install manifest (``VERSION``) versus
   ``rekol.__version__``. The manifest is the only place that can answer it: once
   an install is stale there is nothing else on disk that remembers what was
   installed.
2. **Wiring drift** — are all the hook handlers this version ships actually
   registered in ``settings.json``? This is what catches the real bug, and it is
   deliberately computed LIVE from two independent sources — the shipped hook
   snippets and the user's settings — rather than from a list recorded at install
   time. A recorded list is a second source of truth that goes stale silently;
   #158 was exactly that mistake (a coverage check whose denominator came from
   the thing it was checking).

Network-based "is something newer available?" is a separate concern and is NOT
here — see #27's version-check half.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from rekol import __version__
from rekol.config import resolve_claude_config_dir

MANIFEST_RELPATH = Path(".install-logs") / "manifest.env"

# Keys we are willing to read out of the manifest. Whitelisted and never sourced
# — the file lives in a synced tree and must not be able to execute anything.
_MANIFEST_KEYS = frozenset(
    {
        "TOOLS_HOME",
        "BIN_DIR",
        "REKOL_HOME",
        "SHIM",
        "INDEX_DIR",
        "ARCHIVE_DIR",
        "INSTALLED_AT",
        "VERSION",
        "COMMIT",
    }
)

# Handlers that are deliberately NOT wired by install.sh, so their absence is not
# drift. ``context-watch`` is the opt-in statusline recorder and
# ``stop-failure-record`` is registered by ``rekol resume enable`` — both are the
# user's choice, and nagging about them would train people to ignore this check.
#
# A test asserts this set reconciles the two INDEPENDENT sources (the CLI's hook
# group and the shipped snippets), so adding a handler and forgetting either to
# wire it or to mark it opt-in fails CI instead of shipping silently.
OPT_IN_HANDLERS = frozenset({"context-watch", "stop-failure-record"})

_HANDLER_RE = re.compile(r"_hook ([a-z][a-z-]*)")

# The absolute fallback inside `$(command -v <name> || echo <path>)`. Anchored to
# that substitution so a multi-statement hook command's own `echo` statements
# cannot be mistaken for it.
_FALLBACK_RE = re.compile(
    r"command\s+-v\s+\S+\s*\|\|\s*echo\s+(?:'([^']+)'|\"([^\"]+)\"|([^\s)]+))"
)


@dataclass
class Drift:
    """What is inconsistent between the recorded install and the running code."""

    running_version: str
    installed_version: str | None
    missing_handlers: list[str] = field(default_factory=list)
    installed_at: str | None = None

    @property
    def version_drifted(self) -> bool:
        """True when the recorded install version differs from the running code.

        ``None`` (no ``VERSION`` in the manifest) is NOT drift — it means the
        install predates version stamping, which is reported separately so it
        cannot be confused with a genuine mismatch.
        """
        return self.installed_version is not None and self.installed_version != __version__

    @property
    def version_unknown(self) -> bool:
        """True when the manifest records no version (pre-#27 install)."""
        return self.installed_version is None

    @property
    def has_drift(self) -> bool:
        """True when anything is inconsistent and worth telling the user."""
        return self.version_drifted or bool(self.missing_handlers)


def manifest_path(memory_home: Path) -> Path:
    """Where ``install.sh`` writes the install manifest."""
    return memory_home / MANIFEST_RELPATH


def read_manifest(memory_home: Path) -> dict[str, str]:
    """Parse the install manifest into a dict of whitelisted keys.

    Never sources the file (it sits in a synced tree). Unknown keys and malformed
    lines are skipped rather than raising: a manifest a future version extended
    must not break an older ``doctor``.
    """
    path = manifest_path(memory_home)
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _MANIFEST_KEYS:
            values[key] = value.strip()
    return values


def expected_handlers() -> set[str]:
    """Handlers this version ships and install.sh is expected to wire.

    Derived from the CLI's own hook group rather than from ``hooks/*.json``,
    because the snippets are repo files: a wheel or tarball install has no
    ``hooks/`` directory, and a check that silently finds nothing to compare
    would report "no drift" for every such install — the exact shape of #158.
    The CLI is always present, by definition.

    :func:`snippet_handlers` derives the same set from the snippets, and a test
    asserts the two agree.
    """
    from rekol.cli_hooks import hook_group

    return set(hook_group.commands.keys()) - OPT_IN_HANDLERS


def snippet_handlers(snippet_dir: Path) -> set[str]:
    """Handlers the shipped hook snippets declare.

    The installer's side of the contract, used to cross-check
    :func:`expected_handlers` in tests.
    """
    found: set[str] = set()
    if not snippet_dir.is_dir():
        return found
    for snippet in sorted(snippet_dir.glob("*-snippet.json")):
        try:
            raw = snippet.read_text(encoding="utf-8")
        except OSError:
            continue
        found.update(_HANDLER_RE.findall(raw))
    return found - OPT_IN_HANDLERS


def wired_handlers(settings: dict) -> set[str]:
    """Handlers registered in a parsed ``settings.json`` — by NAME only.

    This answers "is it present?", never "does it work?". Keep the two apart:
    ``✓ all handlers registered`` was reproducibly printed for a settings.json
    where every command pointed at a path that does not exist, because the
    evidence here is text while the property users care about is execution. See
    :func:`unrunnable_hooks` for the half that actually runs something.
    """
    found: set[str] = set()
    for blocks in (settings.get("hooks") or {}).values():
        for block in blocks or []:
            for hook in block.get("hooks") or []:
                found.update(_HANDLER_RE.findall(str(hook.get("command", ""))))
    return found


def _hook_fallback_path(command: str) -> str | None:
    """The absolute path a rendered hook falls back to when PATH lacks rekol.

    #159's fix renders every hook as
    ``"$(command -v rekol || echo '<abs>/rekol')" _hook <name> …``. That absolute
    fallback is the entire guarantee: it is what makes the hook work in the
    non-interactive shell that has no ``.zshrc``. So the fallback is the right
    thing to verify — checking whether ``rekol`` happens to be on *this* shell's
    PATH would pass for a reason the hook cannot rely on.

    Returns ``None`` for a bare ``rekol …`` command, i.e. one with no fallback at
    all — the #159 bug itself.

    Anchored to the ``command -v … || echo …`` substitution, NOT to the first
    ``echo`` in the command. The SessionStart memory-loader is a multi-statement
    shell command containing several ``echo '…'`` calls before the rekol
    invocation, so an unanchored search picked up ``echo '[rekol] memory home not
    configured'`` and reported it as a missing binary — flagging a perfectly
    working install as broken on a real machine. That is the same false-positive
    shape as reporting a working hook "BROKEN" by string comparison.
    """
    match = _FALLBACK_RE.search(command)
    if match:
        return next((g for g in match.groups() if g), None)
    return None


def unrunnable_hooks(settings: dict, probe: bool = True) -> list[tuple[str, str]]:
    """Registered rekol hooks that cannot actually execute: ``[(handler, why)]``.

    The check the whole #159 family needed and never had: *nothing anywhere in
    this repo ever executed a hook it had registered.* Every assertion — the
    installer's jq gates, ``resume status``, and (until now) this module — matched
    strings. So a hook could be present, correctly spelled, and dead.

    Verifies the absolute fallback path resolves to something that can actually
    run, by invoking ``<exe> --version``. That is side-effect free, which matters:
    running the hook commands themselves would record a stop, fire a nudge, or
    launch an indexer as a side effect of asking whether they work.
    """
    problems: list[tuple[str, str]] = []
    for blocks in (settings.get("hooks") or {}).values():
        for block in blocks or []:
            for hook in block.get("hooks") or []:
                command = str(hook.get("command", ""))
                handlers = _HANDLER_RE.findall(command)
                if not handlers:
                    continue
                name = handlers[0]
                fallback = _hook_fallback_path(command)
                if fallback is None:
                    problems.append(
                        (name, "bare invocation with no absolute fallback — exits 127 (#159)")
                    )
                    continue
                exe = Path(os.path.expandvars(fallback))
                if not exe.exists():
                    problems.append((name, f"fallback path does not exist: {exe}"))
                elif not os.access(exe, os.X_OK):
                    problems.append((name, f"fallback path is not executable: {exe}"))
                elif probe and not _runs(exe):
                    problems.append((name, f"fallback path cannot run: {exe} --version failed"))
    return problems


def _runs(exe: Path) -> bool:
    """True when the executable actually runs.

    A console script whose interpreter was replaced (``brew upgrade python``)
    stays executable but cannot import, so existence is not enough.
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [str(exe), "--version"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def load_settings(path: Path | None = None) -> dict:
    """Read Claude Code's settings.json, honouring ``CLAUDE_CONFIG_DIR``.

    Returns ``{}`` when absent or unparseable — a drift check must never be the
    thing that breaks, and "cannot read settings" is reported by the caller as
    *unknown*, never as "no drift".
    """
    target = path or (resolve_claude_config_dir() / "settings.json")
    if not target.is_file():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def detect_drift(memory_home: Path, settings: dict | None = None) -> Drift:
    """Compare the recorded install and this version's handlers against reality."""
    manifest = read_manifest(memory_home)
    settings = load_settings() if settings is None else settings
    missing = sorted(expected_handlers() - wired_handlers(settings))
    return Drift(
        running_version=__version__,
        installed_version=manifest.get("VERSION") or None,
        missing_handlers=missing,
        installed_at=manifest.get("INSTALLED_AT") or None,
    )
