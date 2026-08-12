"""Tests for the hidden rekol _hook time-context / record-stop subcommands."""

import json
from pathlib import Path

from click.testing import CliRunner

from rekol.cli_hooks import hook_group


def _run(args, stdin):
    return CliRunner().invoke(hook_group, args, input=stdin)


def test_time_context_emits_env_time_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = _run(["time-context"], json.dumps({"session_id": "abc-123"}))
    assert res.exit_code == 0
    assert "<env-time>" in res.output and "local_time" in res.output


def test_record_stop_updates_state_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _run(["time-context"], json.dumps({"session_id": "s1"}))
    res = _run(["record-stop"], json.dumps({"session_id": "s1"}))
    assert res.exit_code == 0 and res.output.strip() == ""
    state = json.loads((tmp_path / ".claude" / "session-env" / "time-context-s1.json").read_text())
    assert state["last_assistant_epoch"] is not None


def test_soft_fail_on_bad_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = _run(["time-context"], "not json")
    assert res.exit_code == 0  # never blocks the prompt


def test_soft_fail_on_path_traversal_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = _run(["time-context"], json.dumps({"session_id": "../../evil"}))
    assert res.exit_code == 0
    assert not (tmp_path / "evil.json").exists()


def test_hook_snippets_call_rekol_hook():
    """Snippets invoke the CLI through the @REKOL@ placeholder, not a bare `rekol`.

    A bare `rekol` exits 127 in any hook whose shell didn't inherit an interactive
    PATH (#159), so install.sh renders @REKOL@ into a PATH-independent
    invocation. Asserting the placeholder — not the bare name — is what stops a
    regression back to the broken form.
    """
    repo = Path(__file__).resolve().parents[1]
    ups = json.loads((repo / "hooks" / "userpromptsubmit-snippet.json").read_text())
    assert "@REKOL@" in ups["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "_hook time-context" in ups["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    stop = json.loads((repo / "hooks" / "stop-snippet.json").read_text())
    assert "@REKOL@" in stop["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "_hook record-stop" in stop["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_no_snippet_invokes_a_bare_rekol(tmp_path):
    """#159 regression guard: no shipped snippet may execute a bare `rekol`.

    Prose mentions inside echo strings (backtick-quoted, e.g. run `rekol capture`)
    are fine — those are text for the user, not commands. What must never come
    back is an EXECUTED bare invocation, which is what exited 127.
    """
    import re

    repo = Path(__file__).resolve().parents[1]
    offenders = []
    # command position: start of string, or after a shell separator
    bare = re.compile(r"(?:^|[;&|(]\s*)rekol\s+(?:_hook|review|session-index|capture|search)\b")
    for snippet in sorted((repo / "hooks").glob("*-snippet.json")):
        data = json.loads(snippet.read_text())
        for blocks in data.get("hooks", {}).values():
            for block in blocks:
                for hook in block.get("hooks", []):
                    cmd = hook.get("command", "")
                    # strip backticked prose so `rekol capture` in an echo doesn't trip it
                    stripped = re.sub(r"`[^`]*`", "", cmd)
                    if bare.search(stripped):
                        offenders.append(f"{snippet.name}: {cmd[:80]}")
    assert not offenders, "bare rekol invocation(s) would exit 127 in a hook:\n" + "\n".join(
        offenders
    )


def test_soft_fail_on_non_dict_state_file(tmp_path, monkeypatch):
    # A state file that is valid JSON but not an object must not crash the hook.
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".claude" / "session-env"
    d.mkdir(parents=True)
    (d / "time-context-s2.json").write_text("[1, 2, 3]")
    res = _run(["time-context"], json.dumps({"session_id": "s2"}))
    assert res.exit_code == 0
    res2 = _run(["record-stop"], json.dumps({"session_id": "s2"}))
    assert res2.exit_code == 0


def test_sessionend_snippet_includes_review_nudge():
    repo = Path(__file__).resolve().parents[1]
    snip = json.loads((repo / "hooks" / "sessionend-snippet.json").read_text())
    cmds = [h["command"] for h in snip["hooks"]["SessionEnd"][0]["hooks"]]
    assert any("@REKOL@" in c and "review --nudge" in c for c in cmds)


def test_migration_list_covers_every_shipped_hook_invocation():
    """#159 guardrail: install.sh's migration list must cover every hook we ship.

    Step 6.95 repairs old bare-`rekol` commands by rewriting a hardcoded list of
    subcommand strings. That list is a hand-maintained shadow of what the snippets
    (plus cli_resume.py) actually install. Add a tenth hook and forget the list and
    NOTHING errors — the migration just silently skips it, and the downstream
    idempotency checks then either append a duplicate or skip forever, silently
    reproducing the original bug. So the omission has to fail CI, not a user.
    """
    import re

    repo = Path(__file__).resolve().parents[1]
    install_sh = (repo / "install.sh").read_text()

    # The migration's list, as jq string literals inside the $subs array.
    block = re.search(r"as \$subs", install_sh)
    assert block, "could not locate the migration's $subs array in install.sh"
    window = install_sh[max(0, block.start() - 900) : block.start()]
    migration_subs = set(re.findall(r'"((?:_hook |review |session-index )[^"]+)"', window))
    assert migration_subs, "parsed no entries from the migration list"

    # Every invocation actually shipped in a snippet.
    shipped = set()
    for snippet in sorted((repo / "hooks").glob("*-snippet.json")):
        for cmd in re.findall(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"', snippet.read_text()):
            for m in re.findall(
                r"@REKOL@\\?\"? ((?:_hook |review |session-index )[a-z-]+(?: --[a-z-]+)?)", cmd
            ):
                shipped.add(m.strip())
    assert shipped, "found no @REKOL@ invocations in the shipped snippets"

    # Plus the one cli_resume.py registers itself, which lives outside the snippets.
    from rekol.cli_resume import _HOOK_MARKER

    shipped.add(_HOOK_MARKER)

    missing = sorted(s for s in shipped if s not in migration_subs)
    assert not missing, (
        "these shipped hook invocations are NOT in install.sh's migration list, so an "
        "existing install would never be repaired for them:\n  " + "\n  ".join(missing)
    )


def test_no_snippet_mentions_a_migrated_phrase_as_prose():
    """#159 guardrail: the migration rewrites by substring, so prose is at risk.

    Step 6.95 matches `rekol <subcmd>` after a shell boundary char, with no
    awareness of quoting. Phrases like `review --nudge` and `session-index
    --incremental` are prose-like; if a snippet's echoed message ever mentions one,
    the migration would rewrite that TEXT — and if the match landed inside a
    double-quoted string, the inserted quotes could break the hook's shell syntax.
    Not currently triggered by anything shipped; this keeps it that way.
    """
    import re

    repo = Path(__file__).resolve().parents[1]
    prose_risky = ["review --nudge", "session-index --incremental"]
    offenders = []
    for snippet in sorted((repo / "hooks").glob("*-snippet.json")):
        raw = snippet.read_text()
        for cmd in re.findall(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
            for phrase in prose_risky:
                for m in re.finditer(re.escape(phrase), cmd):
                    # Legitimate use: immediately preceded by the rendered/placeholder
                    # invocation. Anything else is prose the migration could corrupt.
                    before = cmd[max(0, m.start() - 40) : m.start()]
                    if "@REKOL@" not in before:
                        offenders.append(f"{snippet.name}: …{before[-30:]}[{phrase}]…")
    assert not offenders, (
        "phrase(s) the migration rewrites appear outside an invocation — it would "
        "corrupt this text on the next install:\n  " + "\n  ".join(offenders)
    )
