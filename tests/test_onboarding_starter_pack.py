"""Pure onboarding helpers added by T1 (#39): starter-pack seeding + scope argv.

These back the forked ``rekol init``: the starter-pack step (Path B always, Path
A opt-in) seeds the bundled ``template/`` into ``$REKOL_HOME`` *if absent*, and
the bootstrap step translates the shared scope knobs into a ``rekol bootstrap``
argv. Both are pure/deterministic so the interactive shell stays thin.
"""

from __future__ import annotations

import re
from pathlib import Path

from rekol.onboarding import (
    bootstrap_argv,
    find_template_dir,
    seed_starter_pack,
)


def _make_template(root: Path) -> Path:
    """Build a multi-layer stand-in ``template/`` tree (mirrors the bundled one)."""
    template = root / "template"
    (template / "always").mkdir(parents=True)
    (template / "when").mkdir(parents=True)
    (template / "topics").mkdir(parents=True)
    (template / "knowledge").mkdir(parents=True)
    (template / "REKOL.md").write_text("# REKOL\n", encoding="utf-8")
    (template / "always" / "identity.md.example").write_text("# id\n", encoding="utf-8")
    (template / "when" / "when-touching-repos.md.example").write_text("# repos\n", encoding="utf-8")
    (template / "topics" / "example-canonical-source.md.example").write_text(
        "# topic\n", encoding="utf-8"
    )
    (template / "knowledge" / "why-we-chose-x.md.example").write_text("# why\n", encoding="utf-8")
    return template


def test_find_template_dir_locates_bundled_template() -> None:
    """The real bundled ``template/`` ships REKOL.md + layer examples."""
    found = find_template_dir()
    assert found is not None
    assert (found / "REKOL.md").is_file()


def test_find_template_dir_resolves_inside_package_not_repo_root() -> None:
    """#56: the template must resolve as package data (inside the installed rekol
    package) so it survives a wheel install — not via a repo-root path that only
    exists in an editable checkout. Guards against moving it back out of the package.
    """
    import rekol

    found = find_template_dir()
    assert found is not None
    package_dir = Path(rekol.__file__).resolve().parent
    assert found.resolve().is_relative_to(package_dir)


def test_bundled_template_covers_all_four_layers() -> None:
    """Every layer (always/when/topics/knowledge) ships a directive scaffold (#60)."""
    template = find_template_dir()
    assert template is not None
    for layer in ("always", "when", "topics", "knowledge"):
        layer_dir = template / layer
        assert layer_dir.is_dir(), f"missing layer dir: {layer}"
        scaffolds = list(layer_dir.glob("*.example"))
        assert scaffolds, f"layer {layer} ships no scaffold"


def _scaffold_body(text: str) -> str:
    """Return the markdown body of a scaffold, stripping any YAML frontmatter.

    Bracketed ``[...]`` tokens are legal *inside* frontmatter (YAML lists like
    ``tags: [a, b]``); the confabulation hazard is bracketed fact-data in the
    rendered body, so the placeholder check only inspects the body.
    """
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            newline = text.find("\n", end + 1)
            return text[newline + 1 :] if newline != -1 else ""
    return text


def test_bundled_template_carries_no_placeholder_fact_data() -> None:
    """Scaffolds must not embed bracketed fact-data the AI could recall as truth.

    A ``[name]`` / ``[role]`` style token in body text is the confabulation
    hazard #60 exists to kill: the model treats memory body text as fact. The
    scaffolds may carry *directives* ("record the user's name here") but never a
    fill-in slot that looks like a recalled value. We allow markdown link syntax
    ``[text](target)`` — that is navigation, not a fact slot.
    """
    template = find_template_dir()
    assert template is not None
    # A bracketed token NOT immediately followed by "(" — i.e. not a markdown link.
    bracket_token = re.compile(r"\[[^\]]+\](?!\()")
    offenders: list[str] = []
    for scaffold in template.rglob("*.example"):
        body = _scaffold_body(scaffold.read_text(encoding="utf-8"))
        for line in body.splitlines():
            if bracket_token.search(line):
                rel = scaffold.relative_to(template).as_posix()
                offenders.append(f"{rel}: {line.strip()}")
    assert not offenders, "placeholder fact-data found:\n" + "\n".join(offenders)


def test_anatomy_tour_lives_in_docs_not_the_pack() -> None:
    """The anatomy-of-good-memory tour moved out of the pack into docs/ (#60)."""
    template = find_template_dir()
    assert template is not None
    # Not in the seedable pack — it must never land in a user's memory home.
    assert not (template / "knowledge" / "anatomy-of-good-memory.md.example").exists()
    assert not list(template.rglob("anatomy-of-good-memory*"))
    # It lives in the repo docs/ instead (repo-level, not package data — so never
    # shipped in the wheel or seeded). Resolve the repo root from this test file,
    # not from the template dir, which now lives inside the package (#56).
    repo_root = Path(__file__).resolve().parents[1]
    assert (repo_root / "docs" / "anatomy-of-good-memory.md").is_file()


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


def test_seed_starter_pack_gap_fills_only_missing_layers(tmp_path: Path) -> None:
    """A partially-populated home keeps every real file; only missing layers seed.

    This is the gap-fill contract (#60): a user who already learned/recorded
    their identity and a repo convention must not have either replaced by a
    generic scaffold, while the empty layers (topics/, knowledge/) still receive
    their directive scaffolds so the model knows what to grow there.
    """
    template = _make_template(tmp_path / "src")
    home = tmp_path / "home"
    (home / "always").mkdir(parents=True)
    (home / "when").mkdir(parents=True)
    # Two layers the user has already filled with real content.
    user_identity = home / "always" / "identity.md"
    user_identity.write_text("REAL identity — keep me\n", encoding="utf-8")
    user_repos = home / "when" / "when-touching-repos.md"
    user_repos.write_text("REAL repo convention — keep me\n", encoding="utf-8")

    created = seed_starter_pack(template, home)

    # The user's real content is untouched, byte-for-byte.
    assert user_identity.read_text(encoding="utf-8") == "REAL identity — keep me\n"
    assert user_repos.read_text(encoding="utf-8") == "REAL repo convention — keep me\n"
    # The two filled layers were NOT re-seeded...
    assert user_identity not in created
    assert user_repos not in created
    # ...but the missing layers WERE gap-filled with their scaffolds.
    created_rel = {p.relative_to(home).as_posix() for p in created}
    assert "topics/example-canonical-source.md" in created_rel
    assert "knowledge/why-we-chose-x.md" in created_rel
    assert "REKOL.md" in created_rel
    assert (home / "topics" / "example-canonical-source.md").is_file()
    assert (home / "knowledge" / "why-we-chose-x.md").is_file()


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
