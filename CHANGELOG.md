# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project aims to
follow [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- **The library now refuses to replace a real index with test-built data.** On 2026-08-18 a
  `rekol index rebuild` running the **test embedder** replaced a live user's curated index;
  search over curated memory returned nothing for two days and nothing surfaced it, because
  the write *succeeded* — a test-built index is a perfectly valid index whose vectors simply
  mean nothing.
  The existing check could not catch it. `IndexStore.check_model_identity()` compares the
  configured model against the one recorded in the index — but `rebuild` deliberately builds
  into a temp DB and swaps it over `index.db` atomically (so a kill mid-rebuild cannot leave
  an empty index), and **nothing on that path reads the old index's identity**. The guard was
  where it was convenient to assert, not where the destructive act happens.
  New `rekol.safety.assert_not_clobbering_real_index()` runs immediately before the rebuild:
  if the incoming model is a test embedder (`test-hashing`) and the existing index records a
  real one, it refuses and names both. `test-hashing` is a reliable signal precisely because
  nothing legitimate uses it. Escape hatch:
  `REKOL_ALLOW_TEST_EMBEDDER_TO_OVERWRITE_REAL_INDEX=1`.
  **Deliberately not path-based.** Both August incidents involved isolation that was genuinely
  attempted and silently outranked — the sandbox redirected `REKOL_HOME`/`XDG_CACHE_HOME`
  while an inherited `REKOL_INDEX_DIR` (highest precedence, used verbatim) still pointed at
  the real cache. A guard asking *"does this path look like a sandbox?"* would have been
  fooled identically. Asking *"what built the thing I am about to destroy?"* cannot be.
  It **fails open** on a missing, unstamped, or corrupt index: a guard that blocked the
  rebuild which repairs a damaged index would be worse than the bug. Tests cover both
  directions, including an end-to-end reproduction of the incident through the CLI —
  confirmed to fail when the call site is removed while all unit tests still pass, which is
  exactly the failure a unit test cannot see.
  **Known gap, filed not fixed:** the *sessions* index guards on embedding **dimension**
  only, and `test-hashing` is 384-dim like `bge-small-en-v1.5` — so the same mistake against
  `sessions.db` would still pass silently. Closing it requires recording a model identity in
  the sessions schema, which is a larger change than this fix.

### Fixed
- **`uninstall.sh` silently destroyed the transcript archive, in the default layout.** Step 3
  removed the tools home with `rm -rf "${TOOLS_HOME}"` under a comment asserting it was
  *"entirely rekol-owned and rebuildable — safe to remove wholesale."* That premise was false:
  `TOOLS_HOME` defaults to `~/.local/share/rekol` and the archive to
  `${XDG_DATA_HOME:-~/.local/share}/rekol/archive` — **a subdirectory of it**. So an ordinary
  uninstall deleted the archive as collateral: no `--purge-archive`, no prompt, nothing in the
  removal summary, while `--help` promised it was *"preserved by default, never silently
  removed."* The code did the exact opposite of its documented contract.
  This is not hypothetical. It destroyed **759 sessions that existed only in the archive**
  (2026-08-18), unrecoverably — the archive is the durable source of truth a rebuild reads
  *from*, not a cache that can be rebuilt. A guard already existed for the archive overlapping
  `$REKOL_HOME`; nobody wrote one for `TOOLS_HOME`, and unlike that case this is not an exotic
  misconfiguration — **it is the default install**.
  The invariant is now explicit: **Step 3 never deletes the archive; Step 6c is the single
  owner of that decision.** When the archive lives inside the tools home, Step 3 removes the
  other entries and skips the archive *and any ancestor of it*, so a nested
  `TOOLS_HOME/data/archive` survives too; when the archive *is* the tools home, it refuses
  outright and reports it rather than doing something clever. `--purge-archive` still removes
  it when explicitly asked. The `--help` removal list no longer overstates what goes.
  Tests assert both directions — the archive survives a default uninstall and is still removed
  by `--purge-archive` — and were confirmed to **fail against the old code**, so they encode
  the bug rather than the fix.
- **A failing rekol hook could lock you out of Claude Code entirely.** A hook that exits non-zero
  can fail its *event*, and for `UserPromptSubmit` that means **prompts cannot be submitted at
  all** — rekol making the editor unusable, the worst failure this tool has. It happened on a live
  machine: `_hook time-context` shipped without a `|| true`, so any command-level failure (missing
  binary, deleted venv, exit 127) blocked the session.
  The contract was not missing — `cli_hooks.py` states it four times ("a hook must never break the
  session injection") and every handler catches broadly to honour it. But that only guards
  exceptions *inside* the handler: if the **command** fails, the shell returns non-zero and Python
  never runs. **The contract was enforced on one side of the boundary only.** Every shipped snippet
  now ends `2>/dev/null || true`, and `install.sh` migrates existing installs, so machines already
  carrying the unguarded hook are repaired on upgrade rather than only new ones being safe.
  The invariant is *"cannot fail its event"*, not *"ends with `|| true`"* — a command ending in `&`
  is asynchronous and already returns 0, and appending a guard after the `&` would start a separate
  command and corrupt the handler. An earlier revision of this fix did exactly that; the existing
  #135 test asserting the detached form survives is what caught it.
- **Reinstalling appended a second `auto-reindex` handler instead of upgrading in place.** #176
  changed the rendered path from `$HOME/…` to the resolved `TOOLS_HOME` while the idempotency check
  still compared against the *old exact string*, so a reinstall saw "absent" and appended — every
  `Write`/`Edit` then re-indexed twice. Detection now keys on the `auto-reindex.sh` **marker** and
  repairs the command in place, so a future path change upgrades rather than duplicates. This is
  precisely the failure #159's migration exists to prevent, reintroduced by changing command text
  without extending that migration — so the migration now also collapses any duplicate already
  written to disk.
- **The install/uninstall test suites could delete the real session index.** `REKOL_INDEX_DIR` and
  `REKOL_ARCHIVE_DIR` (added by the #164 fix so config reaches hooks) are read at higher precedence
  than the `XDG_CACHE_HOME` the sandbox redirects, so an inherited value from the developer's own
  shell pointed the suite's cleanup at the **live** index — which is not hypothetical: it destroyed
  a real 35,969-message index during development. Both suites now clear those variables, and an
  `assert_sandboxed()` sentry refuses any purge whose target is not inside `$TESTROOT`, so a future
  variable with the same power fails loudly instead of deleting data.
- **`doctor` reported a permanent FTS desync that no rebuild could clear.** Found on a live
  machine: `✗ session FTS: keyword index out of sync: 0 orphaned postings, 1 unindexed messages`,
  every run since 2026-08-05, with `rekol session-index --full` printed as the remedy — and
  running it changed nothing.
  The culprit was **one message whose entire content was `👍`**. FTS5 produces *zero tokens* for
  emoji-only (or whitespace-only) content, so such a row can never appear in the inverted index;
  counting it as "unindexed" reports a desync that does not exist and cannot be fixed. The old
  code rested on an explicit assumption in its own docstring — *"empty content is filtered before
  insert"* — which is true and beside the point: **non-empty is not the same as tokenizable**.
  `fts_consistency()` now excludes rows FTS5 cannot tokenize, determined by asking FTS5 itself
  with the same tokenizer (`porter unicode61`) rather than guessing with a character class, which
  would disagree with the real tokenizer exactly at the edges where this bug lives. The probe only
  runs on rows already missing from the vocab — a handful of scratch inserts, not a corpus scan.
  A genuine desync is still detected: the tests delete a posting for a message that *does*
  tokenize and assert it is still reported, so the false alarm was not traded for a false
  all-clear. That mattered more than the fix — a health check that is always red is one people
  learn to ignore, which then hides the checks that matter.

### Fixed
- **`install.sh` and `uninstall.sh` hardcoded `$HOME/.claude`, ignoring `CLAUDE_CONFIG_DIR`
  (#176).** Last surviving members of the family fixed in v0.5.2 — missed because the sweep that
  found the others was scoped to `src/`. Two paths were affected, and the second is the serious
  one: `SKILL_BASE` (skills installed where nothing looks) and **`SETTINGS_JSON`** — so on a
  relocated tree *every hook* was written into a `settings.json` Claude Code never reads. Both
  reported success, because the files were written; they were simply written somewhere nothing
  loads from. `uninstall.sh` had the identical pair, so it would have reported removing skills and
  hooks it never touched — the detector-wider-than-remover shape it already fixed once.
  The rule is now duplicated in bash deliberately (uninstall must resolve it with the venv already
  deleted, and `SETTINGS_JSON` is needed long before the venv exists) and **a test asserts the
  shell rule and `config.resolve_claude_config_dir()` agree** across unset / empty / whitespace /
  path / path-with-spaces, so the two copies cannot drift.
- **rekol shipped a skill whose YAML frontmatter does not parse (#175).**
  `skill/rekol-bootstrap/skill.md` had an unquoted `: ` inside a plain scalar, so
  `yaml.safe_load` raised `mapping values are not allowed here`. Claude Code's parser is more
  forgiving, so it loaded and the damage stayed invisible — but `description` is the string Claude
  Code relevance-matches on, and anything of ours that parses strictly would silently drop the
  skill. **Third instance of this exact bug**, after a live memory file and a template, so the fix
  ships with a test that `yaml.safe_load`s the frontmatter of *every* file rekol ships
  (`skill/*/*.md`, `src/rekol/template/**/*.md`) and asserts each skill declares a non-empty
  `name` and `description`.
### Added
- **`rekol update` — "is something newer available?" (#27, network half).** The offline half
  shipped in v0.5.2 and answers "is what I have actually installed?"; this answers the other
  question, and the two are deliberately separate modules so a network failure can never suppress
  a drift report.
  - **No server, by construction.** `git ls-remote --tags` against the repo you already cloned
    from — not a rekol.io endpoint and not the GitHub API. With no server of ours there is nothing
    to log, count or identify with; that is a claim a sceptical reader can verify with `tcpdump`.
    (The unauthenticated GitHub API is also 60 req/hr per IP, which trips for a company behind one
    NAT.)
  - **Severity comes from the tag, not from the version jump** — #159 was patch-level and needed
    action. Two encodings, both visible in `ls-remote` (an annotated tag's *message* is not, so
    prose severity would cost a fetch or the API): a suffix, `v0.5.4.High` / `v0.5.4.Critical`, or
    a marker ref alongside a clean semver tag, `severity/critical/v0.5.4`.
  - **Tiers.** Unmarked → total silence. `High` → one quiet SessionStart line. `Critical` → an
    action-required line. Silence is the default because the loud tier only stays meaningful if
    the quiet one exists. Dismissal is per-version: silencing 0.6.0 does not silence 0.7.0.
  - **`rekol update` checks and dismisses. It never installs.** A deliberate limit: an agent that
    can update itself unattended is scheduled remote code execution from a GitHub repo with no
    human in the loop. The agent proposes, you run `./install.sh`.
  - **`doctor` reports the checker itself.** The check must soft-fail so it can never break a
    session, which reintroduces silent failure one level up — so `doctor` shows the last
    *successful* check, keyed on success rather than on the state file existing, because a file
    written by a permanently failing check would otherwise read as health.
  - Four failure modes have tests that fail without the fix: `0.4.10` compared **numerically**,
    not lexically (`"0.4.10" > "0.4.9"` is False, and we are one release from that mattering); a
    **future** timestamp counts as due, so a clock reset cannot wedge the throttle forever;
    "could not reach the network" never collapses into "no releases exist", or an offline machine
    looks permanently current; and the throttle lives in the index dir, never `$TMPDIR`, which
    macOS purges.
  - Opt out with `update_check: false`.

### Fixed
- The bats suites would have made ~22 real network calls per run once the update hook was wired —
  measured at 2.12s each, versus 0.18s and no state written with the check disabled. Every bats
  fixture now pins `update_check: false`, so CI cannot acquire a hidden network dependency. Same
  class as #155, where one unit test silently started downloading the embedding model and turned
  CI red twice on unrelated PRs.

## [0.5.2] - 2026-08-12
### ⚠️ Action required for existing installs
- **If you installed rekol before this version, re-run `install.sh` to get the hook fix
  (#159).** Every prior version wired hooks that invoke a bare `rekol`, which exits 127 in
  any session whose shell lacks an interactive PATH (desktop app, multiplexer, agent). Three
  of the eight failed visibly; **five failed silently**, so features like the review nudge
  simply never ran and nothing said so. The fix cannot reach an existing install on its own —
  there is no self-update yet (#27).

  ```bash
  cd /path/to/rekol && git pull && ./install.sh
  ```

  Re-running is safe and idempotent: it repairs the old hook commands **in place** (writing a
  timestamped `settings.json` backup first), preserves any hooks you added yourself, and does
  not duplicate anything. It also adds handlers shipped since your install.

### Added
- **A test that executes every hook command the installer wrote (#170).** Nothing in this repo
  ever did. `install.sh`'s eight jq gates, `cli_resume.py`'s marker match, `update.py`'s regex and
  all 85 `run` invocations in the bats suite were *string* checks — which is why #159 shipped with
  five hooks silently dead, why the plugin's coexistence guard broke unnoticed, and why a
  "✓ all handlers registered" check reached review while every command pointed at a nonexistent
  path. The new test:
  - runs each command with a PATH that **excludes** `BIN_DIR`, because that is the #159 condition
    — a bare `rekol` must fail there, so an interactive PATH would make the test pass for a reason
    the hooks cannot rely on;
  - takes its environment from `settings.json`'s own `env` block, the only channel that reaches a
    hook subshell — so a variable the installer fails to propagate *fails the test* instead of
    being papered over by the harness exporting it (this is how the missing `REKOL_TOOLS_HOME`
    surfaced);
  - strips only the trailing `2>/dev/null || true` so a real failure surfaces as a real exit code,
    and asserts the strip left something non-empty with no mask remaining — a previous attempt at
    this emptied the command, and `bash -c ""` returns 0;
  - asserts the extracted command count matches the JSON array length, so a command containing a
    newline fails loudly instead of being executed as fragments.
- `tests/test_sessionstart_hook.py` now renders `@REKOL@` and asserts exit codes. It previously
  ran the command **straight from the repo**, so its last segment was `"@REKOL@" _hook
  session-confidence` → exit 127, `bash: @REKOL@: command not found` — masked by the hook's own
  `|| true` and then discarded, because the helper returned only stdout. Three tests passed on a
  substring while the handler never ran. Adds a test that runs the command **unmasked** and
  requires exit 0, plus one that proves an unrendered placeholder really does fail, so the
  rendering cannot be "simplified" away later.
- **Offline drift detection — "is what I have actually installed?" (#27, first half.)** The
  motivating case: a machine ran a *current* checkout while its recorded install was 65 days and
  three minor versions old, and three hook handlers shipped in that window were never registered.
  A version check would have reported "up to date", because the code genuinely was — only
  comparing the shipped handlers against the wiring catches it.
  - `install.sh` now **version-stamps the install**: the manifest records `VERSION` (read from the
    venv, i.e. the code that will actually run) and `COMMIT` (best-effort; a tarball install has no
    git). Previously it recorded `INSTALLED_AT` but never *what* was installed, so nothing on disk
    could answer the question at all. An unresolvable version hard-fails the install rather than
    writing an empty `VERSION`, which a drift check would read as "unknown" forever.
  - `rekol doctor` gains two checks. **hook wiring** lists any handler this version ships that is
    not registered in `settings.json` — a PROBLEM, with `./install.sh` as the remedy. **install
    version** compares the recorded install against the running code.
  - Severity is deliberately asymmetric: missing wiring is a PROBLEM (real, silent feature loss),
    while version drift *alone* is INFO. An editable checkout drifts on every `git pull`, and a
    check that is permanently red is a check nobody reads. "No recorded version" is reported as
    *unknown*, never as a mismatch — a warning that cannot be cleared is worse than none.
  - The expected-handler set is derived from the **CLI's own hook group**, not from
    `hooks/*.json`: those are repo files, so a wheel install has no `hooks/` directory and a check
    reading them would find nothing to compare and report "no drift" for every such install — the
    same shape as #158. A test asserts the CLI-derived set and the snippet-derived set agree, so
    adding a handler and forgetting either to wire it or to mark it opt-in fails CI.
  - Acceptance test (the one that matters): a settings.json wired the **old** way — bare `rekol`,
    no `session-confidence` tail, handlers missing — is run through `install.sh` and asserted to
    end up with every shipped handler registered, zero bare invocations, exactly one memory
    loader, a stamped `VERSION`, and the user's own `env` keys intact. Detection that notifies but
    does not repair would pass every other test and still not fix the problem.
  - Network-based "is something newer available?" is deliberately **not** in this change.

- Claude Code plugin spike (#153): prototype plugin under `plugin/` that declares rekol's
  hooks natively (`hooks/hooks.json`) instead of merging them into the user's
  `settings.json`. Findings in `docs/plugin-spike-findings.md` — 4 of 5 acceptance criteria
  pass with evidence (failure visibility, concurrency under 12 simultaneous cold starts,
  scriptable install, coexistence stand-down); the fifth needs a live install. Surfaced a
  real blocker: the bootstrap needs a package source and rekol is not on PyPI, which makes
  #28 a dependency of the plugin path.

### Fixed
- **A custom `--tools-home` install had every hook dead, and nothing could see it (#170).** Two
  bugs, both found within minutes of adding a test that *runs* the commands the installer writes:
  - `hooks/posttooluse-snippet.json` hardcoded `$HOME/.local/share/rekol/hooks/auto-reindex.sh`
    while `install.sh` symlinks it into `${TOOLS_HOME}/hooks/`. With `--tools-home /custom` the
    PostToolUse hook pointed at a path that does not exist — so auto-reindex, the hook #159's own
    notes called "the only immune hook", was silently dead. Now rendered from a `@TOOLS_HOME@`
    placeholder, and rendering failure aborts the install like `@REKOL@` already did.
  - `bin/rekol` resolves its venv from `REKOL_TOOLS_HOME`, defaulting to
    `$HOME/.local/share/rekol` — and nothing propagated that to hooks. Every hook therefore died
    with `rekol venv not found` even though its command was a correct absolute path to the shim:
    the command was right and the shim could not find its own venv. `install.sh` now writes
    `REKOL_TOOLS_HOME` into `settings.json`'s `env` block beside `REKOL_HOME`.
- **`uninstall.sh` left a hook and an env key behind while reporting them removed.** Its
  `strip_event` list covered SessionStart / PostToolUse / SessionEnd / UserPromptSubmit / Stop but
  **not `StopFailure`** — the one event `rekol resume enable` writes to. So uninstall printed
  "stripped rekol hooks" while leaving a hook wired to the venv it had just deleted. Worse, the
  `HAS_REKOL` detector walks *every* event, so it saw the survivor and each subsequent uninstall
  re-backed-up, re-claimed removal, and stripped nothing: **a detector whose scope is wider than
  its remover's can never confirm its own claim.** Both scopes now match, and the new test asserts
  a second `uninstall.sh` reports "nothing to strip" — making the claim confirmable rather than
  merely printed. (Adding `REKOL_TOOLS_HOME` above would otherwise have created a second leftover
  of exactly this shape.)

- CI now runs the uninstall suite it already gated on: the change filter has named
  `tests/test_uninstall.bats` since it was written, but only `test_install.bats` was ever
  executed — so nothing verified uninstall, or (the part that matters) reinstall-after-uninstall.
  #159 broke exactly that path and CI would have gone green. Two test-portability bugs had to be
  fixed for the suite to pass on Linux: `md5 -q` is macOS-only (the suite could never have run on
  ubuntu), and one assertion in `test_install.bats` compared two empty strings and therefore
  asserted nothing.
- Hooks no longer fail (often silently) when the shell has no interactive PATH (#159):
  hooks read `.zshenv`/`.zprofile` but **not** `.zshrc`, which is where `BIN_DIR` is added —
  so a bare `rekol` in a hook exits 127 whenever a session is launched from the desktop app,
  a multiplexer, or an agent. Three of the eight invocations failed visibly; **five failed
  silently** behind `|| true`, so the review nudge and others simply never ran. Snippets now
  invoke the CLI through an `@REKOL@` placeholder that `install.sh` renders to
  `"$(command -v rekol || echo <BIN_DIR>/rekol)"`, applying the pattern `auto-reindex.sh`
  already used to be the only immune hook. `rekol resume enable` writes the same guarded form.
  A new **migration step repairs existing installs in place** — without it, changing the
  command text would have *appended duplicates* where idempotency keyed on an exact match and
  *skipped forever* where it keyed on a substring, i.e. worse than the bug. Also fixed while
  here: the `SessionStart` merge keyed idempotency on an exact command match, so an install
  predating the `session-confidence` tail would get a **second memory-loader** and inject
  REKOL.md twice — it now classifies and upgrades in place (the #135 Step-7D pattern).
- Opt-in auto-resume (#143) was **inert while reporting itself ENABLED** — five independent
  defects. Recording worked in practice (four real freezes were journalled on a live machine
  between 2026-08-05 and 08-07), but **no resume could ever have happened**: defect 4 below made
  the eligibility gate reject every entry unconditionally, and defect 5 would have launched into
  the wrong project even if it had passed. Anyone who ran `rekol resume enable` before this
  version should re-run it; `enable` now repairs an existing install in place instead of printing
  "already registered" over a hook that cannot execute.
  1. The `StopFailure` hook was registered as a bare `rekol …`, the same defect as #159 but in a
     file #159 never touched, because this hook is written by `resume enable` rather than
     `install.sh`. Hooks run in a non-interactive shell (no `.zshrc`), so it exited 127 — and the
     hook's own `2>/dev/null || true` swallowed it.
  2. The launchd plist copied an **allowlist of variable names** that omitted `REKOL_INDEX_DIR`
     and `XDG_CACHE_HOME`, so a user with `XDG_CACHE_HOME` set had `enable` write the opt-in
     marker to one directory while the tick looked in another, found no marker, and did nothing
     forever. The plist now pins the **already-resolved** absolute paths, so there is nothing left
     for the tick to resolve differently and the next env override cannot reintroduce the bug.
  3. `CLAUDE_CONFIG_DIR` was read nowhere in the package, so a relocated Claude Code config tree
     got the hook written into a `settings.json` Claude Code never reads. Now resolved in one
     place (`config.resolve_claude_config_dir`).
  4. Real `StopFailure` payloads carry `error`, not `error_type` — so the limit-shape gate
     compared `""` and skipped **every** entry unconditionally, meaning even a correctly-wired,
     correctly-firing hook would have resumed nothing. Verified against four captured freezes: the
     payload keys are `agent_id, cwd, effort, error, error_details, hook_event_name,
     last_assistant_message, prompt_id, session_id, transcript_path`, and `error` holds a short
     code (observed: `rate_limit` ×2, `invalid_request` ×2) — not prose, and with **no reset time
     anywhere**, so `parse_reset_time` never fires on this path and the 60-minute fallback is the
     real one. Replayed against those four entries, the old gate classifies 0 as limit-shaped; the
     fix classifies the two `rate_limit` entries as limit-shaped and correctly ignores the two
     `invalid_request` ones.
  5. A resume launched in the **wrong directory**. Claude Code stores transcripts per project
     (`<config>/projects/<escaped-cwd>/<session-id>.jsonl`), so `claude --resume <id>` run from the
     launchd job's directory cannot find the session — it would have failed even once everything
     above was fixed. Every captured payload carries `cwd`; the launcher now uses it, and tolerates
     a project directory that has since been deleted rather than raising inside the tick.
  Also: the launchd job now has `StandardOutPath`/`StandardErrorPath` (launchd was discarding the
  one "LAUNCH FAILED" line that says the feature is broken); the ledger records the launch
  **outcome** as well as the claim, so `status` can no longer count a failed launch as a resume;
  and `status` distinguishes "registered" from "registered but cannot execute".
- Memory files were invisible to search, and `doctor` reported full coverage anyway
  (#157/#158). Measured on a real 65-file store: 62 files should be searchable, **10 were
  not**, and `doctor` printed `✓ curated coverage: 52/52 curated files indexed (none
  rejected)`. Three separate scope gaps, plus the reason nobody could see them:
  - `feedback/` — the behavioural-correction layer — was never walked. Those files were
    reachable *only* via the `MEMORY.md` pointer injected at SessionStart, so any session
    that didn't follow the pointer never saw them. Now indexed (their `type: feedback`
    frontmatter already maps to the `when` layer).
  - Flat `projects/<name>.md` files were never walked — only the nested
    `projects/<slug>/<layer>/` form was. Real stores contain both, since a migrated legacy
    "project" memory lands as a flat file.
  - `tasks/` and the root `MEMORY.md` are now **explicitly** excluded rather than
    accidentally missed, and `doctor` states the excluded count out loud. `tasks/` is
    operational state with its own schema; `MEMORY.md` carries no frontmatter by design, so
    reporting it as a rejection would make `doctor` permanently red over a working file.
  - **The reason it was invisible:** `doctor`'s coverage check and the SessionStart banner
    both derived their denominator from the indexer's own walk, so they compared the index
    against itself and *structurally could not* report a file the indexer never discovered.
    Both now take the denominator from the filesystem. The check also distinguishes
    "rejected" (walked, failed validation) from "NEVER WALKED" (a scope bug in the indexer,
    not a problem with the user's file) — different causes, different remedies. This new
    check immediately found the flat-`projects/` gap that the first pass of the fix missed,
    which is the point of not sharing the walk.
- CI no longer randomly fails on a network call (#155): a unit test in
  `test_invalidate_session.py` built a memory home without a `rekol.config.yaml`, so
  `load_config()` fell back to the real `BAAI/bge-small-en-v1.5` and the test downloaded the
  embedding model from huggingface.co at test time — red twice, on a release PR and a feature
  PR, for reasons unrelated to either change. Pins the hashing embedder like the other 27 test
  files, and CI now runs pytest with `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` so this class of
  bug fails loudly on the offending test instead of intermittently on an innocent PR. The full
  suite passes offline (and ~3x faster).

- **Hooks resolved a different index than your shell, and `doctor` reported health anyway
  (#164).** The index location depends on `XDG_CACHE_HOME` and the archive on `XDG_DATA_HOME`,
  and neither reaches a hook — `install.sh` documents that only `settings.json`'s `env` block
  does, and it carried just `REKOL_HOME`. So a user who exports `XDG_CACHE_HOME` from `.zshrc`
  had **two indexes**: interactive `rekol search` read one while every hook (SessionEnd
  `session-index`, auto-reindex, `session-coverage`) wrote the other. Transcripts went into the
  index nobody searched, search silently returned nothing, `doctor` — running in the user's shell
  — printed the shell's path and "index is healthy", and `uninstall.sh` deleted one and left the
  other on disk. The env block now carries the **already-resolved absolute** `REKOL_INDEX_DIR`
  and `REKOL_ARCHIVE_DIR`, which kills the class rather than the instance: there is nothing left
  for a hook to resolve differently, and the XDG variables need no propagation because their
  effect is already baked in. Same move as the launchd plist fix in #143.
- **`doctor` printed "index is healthy" and exited 0 for an install that had never indexed a
  single transcript (#165).** Same family as #158 — a health check that cannot report the failure
  it exists to catch. Three parts:
  - `claude_projects_dir` hardcoded `~/.claude/projects` and `CLAUDE_CONFIG_DIR` was read nowhere
    in the package. Claude Code relocates its whole tree with that variable, so a relocated
    install pointed at a directory that does not exist — `session-index` exited 2 on **every**
    session end, and because the SessionEnd hook runs it under `>/dev/null 2>&1`, the message and
    the exit code were discarded forever. The default now follows `CLAUDE_CONFIG_DIR`; an explicit
    `claude_projects_dir` in the config still wins, because there the user said where it is.
  - `doctor` never checked that the transcript source exists. It does now, as a PROBLEM with a
    remedy — distinct from "nothing to index yet", because the two need different fixes.
  - "session index: not built yet" was graded INFO unconditionally, and `is_healthy` is *"no
    PROBLEM findings"*. It is now INFO only when the projects dir is genuinely empty; if
    transcripts are sitting there unindexed, session search silently returns nothing and that is
    a PROBLEM.
  Also routes the two remaining hardcoded `~/.claude` sites through the resolver: the
  `session-env` dir (`cli_hooks.py`) and `migrate/discover.py`, which additionally bypassed the
  `claude_projects_dir` config key entirely and used a bare `os.environ["HOME"]` subscript — so
  `rekol migrate auto` reported "nothing to migrate" instead of finding the user's legacy memory.
- Test isolation: the suite reached into the developer's **real** `~/.claude/projects`. The
  autouse fixture now points `CLAUDE_CONFIG_DIR` at a per-test temp dir. The leak was invisible
  while "no session index" was graded INFO; grading it on whether transcripts exist turned it
  into a failing test and revealed that ten test files had the same exposure.
- `uninstall.sh` now removes rekol env keys **by namespace** (`REKOL_*`, plus the legacy
  `MEMORY_HOME`/`MEMORY_TOOLS_HOME`) rather than an enumerated list. The list had to be updated in
  two places whenever a key was added, and the last time one was added exactly one of those places
  was updated — leaving a key behind while the run reported it removed. Detector and remover now
  share one predicate, so they cannot drift and any future `REKOL_*` key is cleaned up without
  anyone remembering to come back.

## [0.4.0] - 2026-08-04
### Added
- Blocked tasks are surfaced at SessionStart (#113 follow-up): `rekol _hook session-tasks`
  previously filtered `blocked` out entirely, so a task blocked by an agent was invisible
  to the next session — the opposite of what a durable "work stopped, needs a decision"
  signal is for. Blocked tasks now lead the injection, show their `--reason` inline, and
  are never capped away by the open-task limit.

### Changed
- README: document the Session Continuity features and tighten the top-fold for
  conversion (product asks). New **"Session continuity"** section covers `rekol task`,
  compaction survival, and opt-in `rekol resume` (the three shipped features were
  previously undocumented — undiscoverable is unshipped); the new commands are listed
  under CLI and `tasks/` under Layout. Top-fold now leads with the **happy path**: the
  one-command Quickstart sits directly under "Why", with the Python/sqlite prerequisites
  collapsed into a `<details>` ("Install failed, or search seems degraded?") instead of
  standing between a first-timer and the one-liner. Adds a license/CI/release badge row
  (release badge is dynamic, so it can't go stale), a soft star nudge, and names Claude
  Code in the tagline rather than "the AI assistant you already use".

### Added
- React to context compaction (#122, Session Continuity batch 3/3): compaction
  preferentially destroys decisions/rationale/conventions, and the loss is silent.
  Three-part posture — **steer**: `docs/compaction.md` ships a paste-ready
  `# Compact Instructions` block for CLAUDE.md; **flush**: a one-time capture nudge
  at 60% context usage (`rekol _hook capture-nudge` on UserPromptSubmit, wired
  idempotently by `install.sh`; fed by the opt-in `rekol _hook context-watch`
  statusline recorder — the statusline JSON is the only documented surface exposing
  `context_window.used_percentage`; silent when unwired); **re-present**: verified
  that SessionStart handlers (REKOL.md + #113 open tasks) re-fire on the documented
  `compact` source, so a compacted session gets its working set back automatically.
  Deliberately NO PreCompact backstop: documented hook output does not reach the
  model from PreCompact, and a reminder the agent never sees is theater.
- Groundwork for auto-resume across usage-limit freezes (#143 **Phase A —
  instrumentation only, not yet announced as a user feature**; the trigger is
  unverified until a real freeze confirms it, and the docs are deliberately held
  until then): `rekol resume enable` registers a Claude Code `StopFailure` hook that
  records every API-error turn-end to a local freeze journal (verbatim payload —
  instrumentation: the docs don't confirm which error type an *account* usage limit
  produces, so Phase A captures everything and the first real freeze supplies ground
  truth for Phase B), plus a launchd watchdog running `rekol resume tick` every 5
  minutes. A tick resumes a frozen session (`claude -p --resume`, detached, appends
  to the same transcript) ONLY when all of: the freeze is limit-shaped, its reset
  time has passed (parsed from "resets 3:45pm" / weekly form, else a 60-minute
  fallback), the #113 task layer shows an `in_progress` task claimed by that session
  (the intent semaphore — idle sessions are never resumed), and the (session,
  freeze) pair isn't already in the idempotency ledger. Cap: one resume per tick.
  **OFF by default** — `enable`/`disable`/`status`/`tick --dry-run`; journal/ledger
  live in the local cache, never the synced tree. #143 stays open for Phase B.
- Cross-session task layer (#113, Session Continuity batch 1/3): durable tasks stored
  one-per-file in `$REKOL_HOME/tasks/` (fully shared across sessions; per-task files so
  concurrent sessions never collide on one file), managed via `rekol task
  add|start|done|block|list`. Every write goes through an optimistic-concurrency (CAS)
  loop — hash on read, re-hash before an atomic temp-file+`os.replace` write, bounded
  retry on a lost race — so same-machine concurrent updates merge instead of clobbering.
  A new `rekol _hook session-tasks` SessionStart handler surfaces open/in_progress tasks
  into every fresh session (capped, silent when none, soft-fail); `install.sh` wires it
  idempotently. `rekol task start --session <id>` records the claiming session — the
  intent semaphore #143's opt-in auto-resume will consume. Design: `docs/task-layer.md`.

## [0.3.0] - 2026-07-23
### Fixed
- Starter-pack template now survives a wheel install (#56): `template/` moved into the
  package (`src/rekol/template/`) and declared as `package-data`, and `find_template_dir()`
  resolves it via `importlib.resources` instead of a repo-root `parents[3]` path that only
  existed in an editable checkout. Verified by building a wheel and confirming all template
  files are vendored under `rekol/template/`. `install.sh` seeds from the new in-tree path.
  Unblocks wheel/Homebrew distribution (#116).

### Added
- SessionStart banner surfaces invisible memory files (#123, part 2): the indexer
  persists the current disk-vs-index gap (count + paths of files rejected at index time)
  to a `skipped.json` manifest in the local cache after every run, and a new
  `rekol _hook session-coverage` handler prints one line at session start when it's
  non-zero — `[rekol] ⚠ N memory files invisible to search — run rekol doctor`. Push,
  don't wait for pull. The manifest reflects the **full** gap (not just a given
  incremental run's skips), so the banner can't flicker off on an unrelated edit, and
  clears to 0 once the files are fixed. Wired as its own SessionStart handler (never
  touches the memory-loader command); `install.sh` adds it idempotently to existing installs.
- `rekol doctor` disk-coverage check (#123, part 1): walks `$REKOL_HOME`'s indexable
  layers, diffs against the curated index, and reports every on-disk `.md` file that is
  **rejected at index time** (invalid frontmatter) with its reason — e.g.
  `topics/foo.md — missing required field 'type'`. These files stay readable on disk but
  are invisible to `rekol search`, and no other check caught them. "Index is healthy" is
  now unclaimable while indexable files are being rejected (exit 1). Transient
  valid-but-unindexed staleness is deliberately not flagged (the next index run clears it).
### Fixed
- Harness-written memory files are no longer silently invisible to search (#123, part 3):
  `parse_file` now falls back to Claude Code's nested `metadata.type` when flat `type` is
  absent, and maps its taxonomy onto rekol layers (`user`→`always`, `feedback`→`when`,
  `project`→`topic`, `reference`→`knowledge`). Genuinely unknown types are still rejected
  (not silently defaulted) so typos surface via `rekol doctor` rather than hiding.
- SessionEnd hook no longer blocks session end or fails with "Hook cancelled" (#135):
  `rekol session-index --incremental` now runs **detached** (`nohup ... &`) so a large
  backlog can't exceed Claude Code's hook timeout — it finishes in the background, and the
  next run catches up if interrupted. `install.sh` **upgrades an existing bare handler in
  place** on reinstall (no duplicate). macOS-safe (no `setsid` dependency).
### Changed
- README "sells in 30 seconds" restructure: lead with a tight **2-step Quickstart**
  (install → "teach it your project (recommended)") and collapse the 11 install.sh flags +
  REKOL_HOME/sync/archive config into a `<details>`, so the simple path isn't buried under the
  options wall. Step 2 uses the calibrated **recommended** (not "optional") framing.
### Fixed
- README Quickstart: the "set up my rekol memory" step is now a clear **step 2**
  (right after install), not an "optional" aside — it read as skippable and confusing.
  Framed as "open a new Claude Code session and say 'set up my rekol memory'", noting
  rekol is then used automatically each session. Also normalized bare "Claude" →
  "Claude Code" (the assistant/product) throughout the README.
### Fixed
- README onboarding accuracy: install **auto-indexes your existing Claude Code history**
  at install (searchable right away) — corrected the Quickstart and "Bring in your history"
  section, which wrongly implied indexing was opt-in / done by `rekol init`. `rekol init` /
  "set up my rekol memory" is reframed as the optional curated-distillation + import step
  (matching the site's A1/A2 framing). Surfaced by a real reinstall.
### Added
- CI now **enforces the per-PR version bump** (#102 part 2): a `version-bump` job fails a
  PR unless its version is ahead of `main` (`scripts/bump_version.py --assert-ahead-of`). A
  status check rather than an auto-push, so it works cleanly with branch protection — you still
  run `bump_version.py` (one command), but forgetting it is now impossible.
### Fixed
- README accuracy pass for the public launch: 'runs on macOS' -> 'macOS and Linux';
  generalized the `~/.zshrc`-only references (`--no-shellrc`, uninstall, post-uninstall) to the
  shell rc for zsh AND bash; softened 'no export in v1' -> 'no export yet'.
### Fixed
- `install.sh` now requires a **venv-capable** Python, not just one with the sqlite
  extension (#launch smoke test). On Debian/Ubuntu the system `python3` can have
  `enable_load_extension` yet lack `ensurepip` (venv is a separate `python3-venv`
  package), so install died mid-`venv`; the probe now skips such interpreters and
  falls through to a venv-capable one, or hard-fails early with the exact `apt install
  python3-venv` fix. Fixes native-Linux install + the hosted CI Linux leg.

## [0.2.0] - 2026-07-09
First public release (quiet go-live; announcement Jul 14). Everything below shipped
during the pre-launch and hold windows.

### Added
- README contact line (`leon@rekol.io`) for questions/feedback.
- rekol skill: a fifth behavioral rule, **"Ask only after searching"** (#35 phase 1) —
  run `rekol search` before asking the user for information you might already have;
  split asks into *knowledge* (look it up) vs *judgment* (ask); ground questions as
  disambiguation over open "how?"; stay silent on a strong hit (precision over nagging).
- `scripts/bump_version.py` (#102): bumps the patch `y` and keeps `pyproject.toml`
  `version` and `src/rekol/__init__.py __version__` in lockstep (refuses on drift).
  `--baseline-ref` skips the bump when the minor/major already changed (a deliberate
  release), `--set X.Y.Z` for an explicit version, `--check` for a dry run. Replaces
  hand-editing the two literals each PR; the post-launch CI bump-on-merge step (still
  part of #102) will call it once CI is on hosted runners.

### Changed
- Install tests (`tests/test_install.bats`) no longer rebuild a venv per test (#78):
  a single shared venv is built once in `setup_file()` and reused via a new opt-in
  `install.sh --skip-deps` (also `REKOL_INSTALL_SKIP_DEPS=1`). Each test used to run a
  full installer → `pip install` pulling torch (731 MB), ~18 min/test and CI
  cancellations; the suite now builds deps once. `--skip-deps` is off by default, so
  production installs are unchanged. Added a per-test timeout safety net.

### Fixed
- `install.sh` now **upgrades an existing pre-#119 hardcoded `REKOL_HOME` rc line
  in place** instead of only adding the guard when absent (QA 20260620-2145). #119
  guarded new installs, but a machine that installed earlier kept its old
  `export REKOL_HOME="<path>"` line forever — the clobber survived for exactly the
  early adopters #119 meant to protect. A re-run now rewrites it to the
  `${REKOL_HOME:-<path>}` form (idempotent); new bats case covers the upgrade.
- Installed `REKOL_HOME` rc export is now a default-if-unset guard
  (`export REKOL_HOME="${REKOL_HOME:-<path>}"`) instead of a hardcoded value (#83).
  A fresh shell still resolves to the installed path (no change for the common
  single-store user), but an inherited `REKOL_HOME` — automation/CI/tests redirecting
  to a throwaway store, or `settings.json` relocating it — now survives re-sourcing the
  rc instead of being silently clobbered back to the baked-in path.
- Search no longer goes silently empty after a curated-index **schema bump** (#97).
  Previously an upgrade that bumped the index schema made `rekol search` exit with
  a stderr-only "run `rekol index rebuild`" message and **empty stdout** — which the
  assistant couldn't tell apart from a legitimate "no results". `rekol search` now
  **self-heals**: on a genuine schema-version mismatch it rebuilds the index in place
  (crash-safe temp-DB swap, offline-first) and returns real results, emitting a
  one-time notice to stderr only so `--json` stdout stays valid. Self-heal is scoped
  to schema bumps — a model-identity mismatch still fails loudly (it must not silently
  re-embed under a different model), and if a rebuild can't run (another index op holds
  the lock, or the cache is read-only) it falls back to the actionable message. The
  background `rekol index update` path intentionally keeps instructing a manual rebuild.

### Changed
- Launch runbook: **launch postponed indefinitely** (external clearance pending,
  new date TBD) — added a prominent "ON HOLD — do not execute" banner and replaced
  the hard "June 16" target with "TBD" so the runbook can't be misread as a live go.
- `.gitignore`: ignore dev-internal session/handoff notes (`SESSION-TODOS.md`,
  `HANDOFF.md`) so they can't be swept into the public repo.

### Changed
- Launch runbook: the public launch is **v0.2.0** (the minor bump per the
  versioning convention) — added the stamp-version-and-tag step before the flip.
- Launch runbook: added a dev-owned step to attach the `rekol.io` custom domain
  to the Cloudflare Pages site *after* the repo is public (kept dark until then
  so "View on GitHub" doesn't 404), plus a note that the site is Direct-Upload —
  a new build needs a manual `wrangler pages deploy` until git auto-deploy is wired.

### Changed
- README accuracy: "Day 1 searchable history" now reflects that install indexes
  your existing Claude Code sessions (searchable right after install), not gated
  on the post-install interview. Install section retitled "macOS & Linux"
  (Ubuntu 24.04 x86_64/arm64 verified + bash shell support).
- **bash shell support**: `install.sh`/`uninstall.sh` now write/remove the PATH +
  `REKOL_HOME` exports in the rc for the user's login shell (`$SHELL`) — `~/.zshrc`
  for zsh, `~/.bashrc` (Linux) or `~/.bash_profile` (macOS) for bash — so the
  `rekol` CLI works in a bash terminal, not only zsh. (Claude Code already got
  `REKOL_HOME` via `settings.json` regardless of shell.)

### Changed
- Pre-public hygiene: removed internal planning docs (`docs/plans/`); Code of
  Conduct contact uses a project address (`conduct@rekol.io`); launch runbook
  gains a privacy/hygiene pre-flip checklist (commit authorship, generic test
  environments, identifier scrub).

### Changed
- Post-install terminal output slimmed to a single call-to-action ("set up my
  rekol memory" in a new Claude Code session), with the two genuine fresh-start
  prerequisites and the local/never-uploaded line. The manual checklist
  (edit identity, `rekol search` verify) and feature explainer move to the
  assistant-led flow / README — the terminal just gets you there.
- README: the "Day 1 searchable" claim is gated on running setup (indexing is
  pull-based, not at install) so the first search isn't empty-by-surprise.
- `--help` opt-out hint now shows valid YAML (`archive_enabled: false`, with the
  space) so a copy-paste actually disables the archive.
- Launch runbook: require the final acceptance run against the *actual* launch
  commit/tag (not an earlier fix commit) before the public flip.
- First-run polish (QA macOS pass): the memory-folder prompt now says you can
  press Enter for the default; `install.sh` picks a suitable Python more reliably
  — it probes `python3.12`/`3.11` and the keg-only `python@3.12`/`@3.11` Homebrew
  prefixes, and **stops early with a clear fix** if no interpreter has Python
  ≥3.11 + `sqlite3.enable_load_extension` (instead of silently degrading search).
- README gains **Prerequisites** and **Troubleshooting** sections (Python/sqlite
  extension, keg-only Homebrew, Intel-mac NumPy, `rekol doctor --deep`).

### Added
- **Memory confidence metadata (#87):** `rekol confirm <file>` (stamp
  `last_confirmed`, distinct from an edit) and `rekol flag-suspect <file>
  --reason` (mark a contradiction without rewriting), completing a
  live → suspect → invalid lifecycle. Search hits now show a confidence tag
  (`· confirmed Nago` / `· unconfirmed` / `⚠ suspected (since X — reason)`), and
  the always-on SessionStart injection gains a footer flagging unverified
  always-on facts so the agent hedges before asserting them. Surface-only —
  rekol shows the signal, the agent decides.
- **`rekol doctor --deep`:** post-install acceptance probe — verifies the
  embedding model loads + embeds meaningfully (catches the silent mean-pooling
  degradation class) and that end-to-end curated recall works.
- `.github/FUNDING.yml` (GitHub Sponsors button).

### Changed
- The bootstrap/propose **pending-review queue moved to the local-only cache**
  (out of the synced `REKOL_HOME`), so raw transcript candidates never reach a
  sync provider (#57).
- The embedding model now **loads offline-first** (`local_files_only`) — no
  online HuggingFace check on every search; works offline and under corporate
  TLS interception, and won't silently fall back to a degraded model.
- Hardened secret-shape detection in the bootstrap review gate (Slack, JWT,
  OAuth Bearer tokens).
- Removed dead sqlite-vec setup from the curated `IndexStore`; documented its
  search as a deliberate full numpy scan (vec0 KNN tracked in #90).

### Fixed
- `install.sh` crashed on its final index/session/archive steps when the memory
  home was answered via the prompt (`REKOL_HOME` not exported) — the resolved
  home is now exported once for all subprocesses (#99).
- Intel (x86_64) macOS install crashed on the first embedding (NumPy 2.x vs the
  older torch ABI) — NumPy is capped to `<2` on Intel macs, and `rekol doctor
  --deep` surfaces the exact remedy (#101).

## [0.1.0] - 2026-06-09
### Added
- Open-source scaffolding: Apache-2.0 license, README, CONTRIBUTING, Code of
  Conduct, issue/PR templates.
- Quality gate: Ruff (lint+format), mypy, pre-commit, GitHub Actions CI.

### Genericization & onboarding (Plan 2)
- Data-level names branded REKOL (`rekol.config.yaml`, `REKOL.md`, `rekol`
  skill) with back-compat reads of the legacy names (`memory.config.yaml`,
  `MEMORY.md`, `/memory` shim).
- `scope: private` frontmatter field reserved (parsed but not validated in v0.1; any value is accepted so existing files are never dropped from the index).
- Legacy migration is now opt-in (`install.sh --migrate`).
- `rekol import` gained `--include`/`--exclude` for file-type selection.
- Sync reframed as local-first; `REKOL_HOME` is any folder you own.
- New `rekol init` interactive onboarding (transcript indexing, corpus import,
  cloud-sync detection, opt-in migration).

### Changed
- Rebranded the project from `memory-tools` to **REKOL**: the Python package is
  now `rekol`, and the formerly separate `memory-*` console scripts are unified
  under a single `rekol` command (`rekol search`, `rekol index`, `rekol capture`,
  `rekol import`, etc.). Docs, hooks, skill, and templates are rebranded to match.
- The data-directory env var is now `REKOL_HOME`. `MEMORY_HOME` is still accepted
  as a fallback, so existing installs keep working without changes.

### Fixed
- `rekol search` crash on queries containing FTS5 operator characters
  (e.g. hyphens) — queries are now sanitized into safe FTS5 phrases.
