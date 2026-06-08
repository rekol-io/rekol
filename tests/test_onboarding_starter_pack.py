"""Pure onboarding helpers added by T1 (#39): starter-pack seeding + scope argv.

These back the forked ``rekol init``: the starter-pack step (Path B always, Path
A opt-in) seeds the bundled ``template/`` into ``$REKOL_HOME`` *if absent*, and
the bootstrap step translates the shared scope knobs into a ``rekol bootstrap``
argv. Both are pure/deterministic so the interactive shell stays thin.
"""

from __future__ import annotations

from pathlib import Path

from rekol.onboarding import (
    bootstrap_argv,
    find_template_dir,
    seed_starter_pack,
)


def _make_template(root: Path) -> Path:
    """Build a minimal stand-in ``template/`` tree (mirrors the bundled one)."""
    template = root / "template"
    (template / "always").mkdir(parents=True)
    (template / "when").mkdir(parents=True)
    (template / "REKOL.md").write_text("# REKOL\n", encoding="utf-8")
    (template / "always" / "identity.md.example").write_text("# id\n", encoding="utf-8")
    (template / "when" / "when-touching-repos.md.example").write_text("# repos\n", encoding="utf-8")
    return template


def test_find_template_dir_locates_bundled_template() -> None:
    """The real bundled ``template/`` ships REKOL.md + layer examples."""
    found = find_template_dir()
    assert found is not None
    assert (found / "REKOL.md").is_file()


def test_seed_starter_pack_copies_and_strips_example(tmp_path: Path) -> None:
    """Seeding an empty home copies the template, stripping the .example suffix."""
    template = _make_template(tmp_path / "src")
    home = tmp_path / "home"
    home.mkdir()

    created = seed_starter_pack(template, home)

    # .example suffix stripped on disk.
    assert (home / "REKOL.md").is_file()
    assert (home / "always" / "identity.md").is_file()
    assert not (home / "always" / "identity.md.example").exists()
    # Every seeded file is reported (real names), so the caller can summarise.
    created_names = {p.name for p in created}
    assert "REKOL.md" in created_names
    assert "identity.md" in created_names


def test_seed_starter_pack_never_overwrites_existing(tmp_path: Path) -> None:
    """Re-seeding is a safe no-op for files that already exist (re-runnable)."""
    template = _make_template(tmp_path / "src")
    home = tmp_path / "home"
    (home / "always").mkdir(parents=True)
    # A user file that collides with a template file's real (stripped) name.
    user_identity = home / "always" / "identity.md"
    user_identity.write_text("MY OWN IDENTITY — do not clobber\n", encoding="utf-8")

    created = seed_starter_pack(template, home)

    assert user_identity.read_text(encoding="utf-8") == "MY OWN IDENTITY — do not clobber\n"
    assert user_identity not in created  # not reported as created


def test_seed_starter_pack_second_run_creates_nothing(tmp_path: Path) -> None:
    """Two consecutive seeds: the second reports zero files (idempotent)."""
    template = _make_template(tmp_path / "src")
    home = tmp_path / "home"
    home.mkdir()

    first = seed_starter_pack(template, home)
    second = seed_starter_pack(template, home)

    assert first  # first run seeded something
    assert second == []  # second run is a pure no-op


def test_bootstrap_argv_default_is_bare(tmp_path: Path) -> None:
    """No scope inputs → a bare ``bootstrap`` argv (T3's bounded default applies)."""
    assert bootstrap_argv() == ["bootstrap"]


def test_bootstrap_argv_threads_scope_flags() -> None:
    """Scope inputs map onto T3's exact flag names (reconciled knob shape)."""
    argv = bootstrap_argv(
        all_time=False,
        scope_days=30,
        scope_projects=("rekol", "infra"),
        max_sessions=50,
    )
    assert argv[0] == "bootstrap"
    assert "--scope-days" in argv and "30" in argv
    assert "--max-sessions" in argv and "50" in argv
    # Repeatable project flag, one occurrence per slug.
    assert argv.count("--scope-project") == 2
    assert "rekol" in argv and "infra" in argv
    assert "--all-time" not in argv


def test_bootstrap_argv_all_time_omits_scope_days() -> None:
    """``--all-time`` widens the window and suppresses a redundant --scope-days."""
    argv = bootstrap_argv(all_time=True, scope_days=30)
    assert "--all-time" in argv
    assert "--scope-days" not in argv  # all-time overrides recency, don't double-send
