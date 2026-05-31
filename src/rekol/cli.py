"""Unified `rekol` command-line entrypoint.

Collapses the formerly separate console scripts into one Click group so the
tool presents a single `rekol <subcommand>` surface (REKOL — rekol.io).
"""

from __future__ import annotations

import click

from rekol.cli_capture import main as capture_cmd
from rekol.cli_docs_convert import main as import_cmd
from rekol.cli_index import main as index_grp
from rekol.cli_init import main as init_cmd
from rekol.cli_invalidate import main as invalidate_cmd
from rekol.cli_migrate import main as migrate_grp
from rekol.cli_propose import main as propose_cmd
from rekol.cli_search import main as search_cmd
from rekol.cli_session_index import main as session_index_cmd


@click.group()
@click.version_option(package_name="rekol")
def main() -> None:
    """REKOL — layered, cross-indexed memory with local vector search."""


# Leaf commands keep their own option/argument definitions; register under
# rebranded names. The two Click *groups* (index, migrate) nest, preserving
# their existing subverbs (e.g. `rekol index rebuild`).
main.add_command(search_cmd, name="search")
main.add_command(index_grp, name="index")
main.add_command(capture_cmd, name="capture")
main.add_command(invalidate_cmd, name="invalidate")
main.add_command(propose_cmd, name="propose")
main.add_command(migrate_grp, name="migrate")
main.add_command(session_index_cmd, name="session-index")
main.add_command(import_cmd, name="import")
main.add_command(init_cmd, name="init")


# Enables `python -m rekol.cli` (used by the bin/rekol shim). The installed
# console-script entrypoint does not need this guard, but the `-m` invocation does.
if __name__ == "__main__":
    main()
