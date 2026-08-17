"""Is something newer available? — the network half of #27.

Deliberately separate from :mod:`rekol.update`, which answers the *offline*
question ("is what I have actually installed?"). Keeping them apart is not
tidiness: the offline check must keep working when this one cannot run at all,
and merging them would let a network failure suppress a drift report.

Three design constraints, each from a real failure:

* **No server.** The check is ``git ls-remote --tags`` against the repo the user
  already cloned from — not a rekol.io endpoint and not the GitHub API. With no
  server of ours there is nothing to log, count or identify with; that is a
  claim a sceptical reader can verify with ``tcpdump``. The unauthenticated
  GitHub API is also 60 req/hr per IP, which trips for a company behind one NAT.
* **Semver, never strings.** ``0.4.10`` sorts BELOW ``0.4.9`` lexically, so a
  string compare would silently stop notifying after the tenth patch.
* **Offline must be silent, not slow.** A captive portal or a dead network must
  cost a bounded timeout and produce no output — never a stalled session start.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

CHECK_STATE_NAME = "update-check.json"

# How long between network checks. The throttle lives in the local cache dir,
# NOT $TMPDIR — macOS purges that after ~3 days, which is exactly how a July
# lane-watcher lost its seen-set and re-emitted its entire history at 3am.
CHECK_INTERVAL_HOURS = 24

# Hard ceiling on the network call. Past this we give up silently: a session
# start that stalls is worse than one that does not mention an update.
FETCH_TIMEOUT_SECONDS = 5

# doctor complains when no check has SUCCEEDED in this long — so a checker that
# is quietly broken becomes visible instead of just staying quiet.
STALE_AFTER_DAYS = 14

SEVERITY_NONE = "none"
SEVERITY_HIGH = "high"
SEVERITY_CRITICAL = "critical"
_SEVERITY_RANK = {SEVERITY_NONE: 0, SEVERITY_HIGH: 1, SEVERITY_CRITICAL: 2}

# Two accepted encodings, both visible in `ls-remote` output — which is the whole
# point, because that is the only thing the cheap check can see. An annotated
# tag's MESSAGE is in the tag object, not the ref list, so severity in prose
# would cost a fetch or the API.
#
#   refs/tags/v1.2.3                    → 1.2.3, no severity
#   refs/tags/v1.2.3.High               → 1.2.3, high     (suffix form)
#   refs/tags/severity/critical/v1.2.3  → 1.2.3, critical (marker-ref form)
#
# The marker-ref form keeps the release tag itself pure semver, so every existing
# tool (including scripts/bump_version.py) keeps working; the suffix form is
# accepted because it is one tag instead of two.
_RELEASE_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\.(high|critical))?$", re.IGNORECASE)
_MARKER_RE = re.compile(r"^severity/(high|critical)/v?(\d+)\.(\d+)\.(\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Release:
    """A released version and how loudly it should be announced."""

    version: tuple[int, int, int]
    severity: str = SEVERITY_NONE

    @property
    def text(self) -> str:
        """The version as ``X.Y.Z``."""
        return "{}.{}.{}".format(*self.version)

    def louder_than(self, other: str) -> bool:
        """True when this release outranks the given severity level."""
        return _SEVERITY_RANK[self.severity] > _SEVERITY_RANK[other]


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``"0.4.10"`` → ``(0, 4, 10)``; None when it is not an X.Y.Z version.

    Returns a tuple so comparison is numeric. This is the whole defence against
    the lexical bug: ``"0.4.10" > "0.4.9"`` is False, ``(0,4,10) > (0,4,9)`` is
    True, and the project is already past 0.5.x so the trap is live, not
    theoretical.
    """
    match = _RELEASE_RE.match(text.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def parse_refs(ls_remote_output: str) -> list[Release]:
    """Every release visible in ``git ls-remote --tags`` output, severity merged.

    Peeled refs (``^{}``) are skipped — they are the same tag dereferenced, and
    counting them would double every annotated release.
    """
    found: dict[tuple[int, int, int], str] = {}
    for line in ls_remote_output.splitlines():
        parts = line.split("refs/tags/", 1)
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if ref.endswith("^{}"):
            continue

        marker = _MARKER_RE.match(ref)
        if marker:
            sev = marker.group(1).lower()
            version = (int(marker.group(2)), int(marker.group(3)), int(marker.group(4)))
        else:
            rel = _RELEASE_RE.match(ref)
            if not rel:
                continue
            sev = (rel.group(4) or SEVERITY_NONE).lower()
            version = (int(rel.group(1)), int(rel.group(2)), int(rel.group(3)))

        # A marker ref and its release tag describe one release; keep the loudest.
        current = found.get(version, SEVERITY_NONE)
        if _SEVERITY_RANK[sev] > _SEVERITY_RANK[current]:
            found[version] = sev
        else:
            found.setdefault(version, current)
    return [Release(version=v, severity=s) for v, s in sorted(found.items())]


def newest(releases: list[Release]) -> Release | None:
    """The highest version, by numeric comparison."""
    return max(releases, key=lambda r: r.version) if releases else None


def fetch_releases(repo_dir: Path, timeout: int = FETCH_TIMEOUT_SECONDS) -> list[Release] | None:
    """Ask the origin for its tags. ``None`` means "could not check".

    ``None`` is deliberately distinct from ``[]``: "the network was unreachable"
    must never be recorded as "there are no releases", or a permanently offline
    machine would look permanently up to date. Every failure mode — no git, not a
    repo, no remote, DNS failure, captive portal, timeout — collapses to None,
    silently and within ``timeout``.
    """
    try:
        result = subprocess.run(  # noqa: S603,S607 — fixed argv, no shell
            ["git", "ls-remote", "--tags", "origin"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_refs(result.stdout)


def state_path(index_dir: Path) -> Path:
    """Where the throttle + last-successful-check state lives (local cache)."""
    return index_dir / CHECK_STATE_NAME


def read_state(index_dir: Path) -> dict:
    """Throttle state, or ``{}`` when absent/corrupt — never raises."""
    path = state_path(index_dir)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(index_dir: Path, state: dict) -> None:
    """Persist throttle state. Best-effort: a cache write must never break a hook."""
    try:
        index_dir.mkdir(parents=True, exist_ok=True)
        state_path(index_dir).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return


def due_for_check(
    state: dict, now: _dt.datetime, interval_hours: int = CHECK_INTERVAL_HOURS
) -> bool:
    """Has the throttle window elapsed?

    A timestamp in the FUTURE counts as due. A naive ``now - last > window``
    suppresses the check forever after a clock reset, a DST edge or a restored
    backup — silent non-checking, reached by a different road than a broken
    hook, and indistinguishable from it afterwards. An unparseable timestamp is
    treated the same way: check, and overwrite the bad value.
    """
    raw = state.get("last_attempt")
    if not raw:
        return True
    try:
        last = _dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if last > now:
        return True  # clock skew — never let it wedge
    return (now - last) >= _dt.timedelta(hours=interval_hours)


def last_success(state: dict) -> _dt.datetime | None:
    """When a check last actually reached the network, if ever."""
    raw = state.get("last_success")
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def is_dismissed(state: dict, release: Release) -> bool:
    """Has the user silenced this specific version?

    Per-version by design: "ignore till next time" must stop THIS release
    nagging without silencing every future one. A permanent dismissal is how a
    notification channel dies quietly.
    """
    return str(state.get("dismissed_version") or "") == release.text


def repo_dir() -> Path | None:
    """The git checkout rekol is running from, or None.

    rekol installs from a checkout, so the origin the user cloned from is the
    thing to ask about new versions — no server of ours, and no GitHub API. A
    wheel install has no repo; that resolves to None and the check degrades to
    "cannot check", never to "up to date".
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / ".git").exists():
            return candidate
    return None


@dataclass
class UpdateStatus:
    """The answer to "is something newer available?", plus how we know."""

    current: tuple[int, int, int]
    latest: Release | None = None
    checked_now: bool = False
    last_success: _dt.datetime | None = None
    dismissed: bool = False
    reason: str = ""

    @property
    def update_available(self) -> bool:
        """True only when a strictly newer version is known. Numeric compare."""
        return self.latest is not None and self.latest.version > self.current

    @property
    def severity(self) -> str:
        """Severity of the available update, or ``none``."""
        return self.latest.severity if (self.latest and self.update_available) else SEVERITY_NONE

    @property
    def should_announce(self) -> bool:
        """Whether this warrants interrupting the user.

        Silence is the default and the loud tier only stays meaningful if the
        quiet one exists: an unmarked release is never announced, and a
        dismissed version is never announced again.
        """
        return (
            self.update_available
            and not self.dismissed
            and self.severity in (SEVERITY_HIGH, SEVERITY_CRITICAL)
        )

    def is_stale(self, now: _dt.datetime, after_days: int = STALE_AFTER_DAYS) -> bool:
        """True when no check has SUCCEEDED recently.

        The backstop for the whole mechanism. The check must soft-fail so it can
        never break a session, which reintroduces silent failure one level up —
        so "we have not managed to check in weeks" has to be visible somewhere,
        and that somewhere is `doctor`. Keyed on last SUCCESS, not on the state
        file merely existing, or a permanently failing check looks healthy.
        """
        if self.last_success is None:
            return True
        if self.last_success > now:
            return False  # clock skew: do not manufacture staleness either
        return (now - self.last_success) >= _dt.timedelta(days=after_days)


def check_for_update(
    current: tuple[int, int, int],
    index_dir: Path,
    *,
    repo: Path | None = None,
    force: bool = False,
    now: _dt.datetime | None = None,
    fetcher=None,
) -> UpdateStatus:
    """Throttled "is something newer available?".

    Records the attempt whether or not it succeeded, so a machine that cannot
    reach the network does not retry on every single session — and records the
    LAST KNOWN latest release, so a throttled call still reports accurately
    instead of pretending it knows nothing.
    """
    now = now or _dt.datetime.now()
    state = read_state(index_dir)
    fetch = fetcher or fetch_releases

    cached = state.get("latest")
    cached_release: Release | None = None
    if isinstance(cached, dict):
        parsed = parse_version(str(cached.get("version", "")))
        if parsed:
            cached_release = Release(parsed, str(cached.get("severity", SEVERITY_NONE)))

    status = UpdateStatus(
        current=current,
        latest=cached_release,
        last_success=last_success(state),
    )

    if not force and not due_for_check(state, now):
        status.reason = "throttled"
        status.dismissed = cached_release is not None and is_dismissed(state, cached_release)
        return status

    target = repo or repo_dir()
    if target is None:
        state["last_attempt"] = now.isoformat(timespec="seconds")
        write_state(index_dir, state)
        status.reason = "no git checkout to check against"
        return status

    releases = fetch(target)
    state["last_attempt"] = now.isoformat(timespec="seconds")
    if releases is None:
        write_state(index_dir, state)
        status.reason = "network unreachable"
        return status

    status.checked_now = True
    status.last_success = now
    state["last_success"] = now.isoformat(timespec="seconds")

    found = newest(releases)
    if found is not None:
        status.latest = found
        state["latest"] = {"version": found.text, "severity": found.severity}
    write_state(index_dir, state)

    status.dismissed = status.latest is not None and is_dismissed(state, status.latest)
    return status


def dismiss(index_dir: Path, version_text: str) -> None:
    """Silence one specific version. The NEXT release still notifies."""
    state = read_state(index_dir)
    state["dismissed_version"] = version_text
    write_state(index_dir, state)
