"""rekol coverage: print the include-scope coverage line (T8 #63 scope 4).

A focused, single-purpose surface — the "Z of ~Y discoverable files indexed
(~C%)" signal the onboarding adaptive-close prints — separate from the broader
``rekol doctor`` health report (which also includes it as one finding). Useful as
a quick "how much of my included knowledge is actually searchable?" check.

Exit code is always 0: coverage is informational, not a health gate. (Use
``rekol doctor`` for the pass/fail health surface.)
"""

from __future__ import annotations

import click

from rekol.config import load_config
from rekol.include_coverage import compute_coverage


@click.command(name="coverage")
def main() -> None:
    """Report indexed-vs-discoverable coverage for the configured include-scope."""
    cfg = load_config()
    if not cfg.include_dirs:
        # Additive feature: no include-scope configured is a normal state, not an
        # error. Steer the user to how to add one rather than printing a bare 0%.
        click.echo(
            "no include-scope configured — add directories under `include_dirs` in "
            "rekol.config.yaml (or via onboarding) to index external knowledge."
        )
        return
    report = compute_coverage(cfg)
    click.echo(report.summary())


if __name__ == "__main__":
    main()
