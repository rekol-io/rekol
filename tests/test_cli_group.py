"""Tests for the unified `rekol` Click command group.

These assert that all eight formerly separate console scripts are accounted for
under a single top-level group, with the two Click *groups* (index, migrate)
nested so their subverbs (e.g. `rekol index rebuild`) keep working.
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from rekol.cli import main as rekol_main

EXPECTED_COMMANDS = {
    "search",
    "index",
    "capture",
    "invalidate",
    "propose",
    "bootstrap",
    "review",
    "migrate",
    "session-index",
    "archive",
    "import",
    "init",
    "doctor",
    "coverage",
}

HIDDEN_COMMANDS = {"_hook"}  # registered but hidden from --help


def test_main_is_a_click_group() -> None:
    assert isinstance(rekol_main, click.Group)


def test_registered_command_names_match_exactly() -> None:
    # This set is the automated guard that every console script is accounted for.
    assert set(rekol_main.commands.keys()) == EXPECTED_COMMANDS | HIDDEN_COMMANDS


def test_hook_group_is_hidden_from_help() -> None:
    result = CliRunner().invoke(rekol_main, ["--help"])
    assert "_hook" not in result.output


def test_help_exits_zero_and_lists_subcommands() -> None:
    result = CliRunner().invoke(rekol_main, ["--help"])
    assert result.exit_code == 0
    for name in EXPECTED_COMMANDS:
        assert name in result.output


def test_search_help_smoke() -> None:
    result = CliRunner().invoke(rekol_main, ["search", "--help"])
    assert result.exit_code == 0


def test_index_nested_group_help_mentions_rebuild() -> None:
    result = CliRunner().invoke(rekol_main, ["index", "--help"])
    assert result.exit_code == 0
    assert "rebuild" in result.output
