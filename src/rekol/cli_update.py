"""``rekol update`` — is something newer available? (#27, network half).

**Check and dismiss only. This command never installs anything.**

That is a deliberate limit, not an unfinished one. An agent that can update
itself unattended is scheduled remote code execution from a GitHub repo with no
human in the loop — the same supply-chain concern raised against the unverified
tarball in the plugin spike, sharper here because the absence of a human is the
whole point. So the agent proposes and the human applies: this reports, and the
user runs ``./install.sh`` if they want it.
"""

from __future__ import annotations

import datetime as _dt
import json as _json

import click

from rekol import __version__
from rekol.config import load_config
from rekol.release import (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    check_for_update,
    dismiss,
    parse_version,
    repo_dir,
)

_SEVERITY_LABEL = {
    SEVERITY_CRITICAL: "CRITICAL — update is strongly recommended",
    SEVERITY_HIGH: "high — worth updating soon",
}


@click.command(name="update")
@click.option("--check", "check_only", is_flag=True, help="Report whether a newer version exists.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (implies --check).")
@click.option("--force", is_flag=True, help="Ignore the 24h throttle and check now.")
@click.option(
    "--dismiss",
    "dismiss_it",
    is_flag=True,
    help="Stop announcing the currently-available version (the next one still notifies).",
)
def main(check_only: bool, as_json: bool, force: bool, dismiss_it: bool) -> None:
    """Report whether a newer rekol has been released. Never installs."""
    cfg = load_config()
    current = parse_version(__version__) or (0, 0, 0)
    status = check_for_update(current, cfg.index_dir, force=force or as_json or dismiss_it)

    if dismiss_it:
        if status.latest is None or not status.update_available:
            click.echo("nothing to dismiss — no newer version is known")
            return
        dismiss(cfg.index_dir, status.latest.text)
        click.echo(f"dismissed {status.latest.text}; the next release will still be announced")
        return

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "current": __version__,
                    "latest": status.latest.text if status.latest else None,
                    "severity": status.severity,
                    "update_available": status.update_available,
                    "action_required": status.severity == SEVERITY_CRITICAL,
                    "dismissed": status.dismissed,
                    "checked_now": status.checked_now,
                    # Distinguish "checked, nothing new" from "could not check".
                    # Collapsing them is how an offline machine looks up to date.
                    "reason": status.reason or "ok",
                    "last_success": (
                        status.last_success.isoformat(timespec="seconds")
                        if status.last_success
                        else None
                    ),
                },
                indent=2,
            )
        )
        return

    click.echo(f"installed: {__version__}")
    if status.latest is None:
        click.echo(f"latest:    unknown ({status.reason or 'no releases found'})")
        if repo_dir() is None:
            click.echo("           (no git checkout — rekol installs from a clone)")
        return

    click.echo(f"latest:    {status.latest.text}")
    if not status.update_available:
        click.echo("you are up to date.")
    else:
        label = _SEVERITY_LABEL.get(status.severity)
        click.echo(f"→ {status.latest.text} is available" + (f" · {label}" if label else ""))
        if status.dismissed:
            click.echo("  (dismissed — run with --force to see it again)")
        click.echo("  update with:  cd <your rekol clone> && git pull && ./install.sh")

    if status.is_stale(_dt.datetime.now()):
        click.echo("⚠ no successful update check recently — see `rekol doctor`")
    _ = check_only  # accepted for symmetry with the documented CLI; reporting is the default
