# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project aims to
follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.7.0] - 2026-11-08
### Added
- **`migrate repo` now leaves a repository's own `MEMORY.md` alone.** The only justification for
  touching that file is Claude Code's autoMemory, which injects
  `~/.claude/projects/<slug>/memory/MEMORY.md` into every session — so a migrated auto-memory
  directory must not leave one behind. Outside that tree the file is quite possibly tracked by the
  user's own repo (rekol ships one in its template), where archiving it would relocate tracked
  content and drop an untracked `old-memory-archive/` into their working copy. It is now left in
  place and **said so explicitly**, not merely skipped in silence — silence would be
  indistinguishable from the behaviour that moved it.
  The decision keys on *where the directory is*, not which subcommand was typed, so `migrate repo`
  pointed at a real auto-memory dir and `migrate auto` reaching the same dir agree about it. A
  relocated `CLAUDE_CONFIG_DIR` is honoured (#165).
- **`doctor` now classifies every `rekol` on PATH by static inspection, and probes what the hooks
  actually recorded (#203).** A console script can be left pointing at an interpreter where the
  package no longer resolves, so every call through it dies with
  `ModuleNotFoundError: No module named 'rekol'`. An interactive shell resolves the working
  wrapper first, so `command -v rekol` is green and `doctor` passed — while a **hook**, whose PATH
  order differs, resolves the orphan and the session starts with no memory and nothing saying so.
  **No program found on PATH is executed.** `--version` is not a safety boundary for an arbitrary
  program named `rekol`: it can mutate state, read stdin, spawn children or hang, and bounding
  each candidate still let a scripted `doctor` stall for minutes. (The one thing that *is*
  executed is a path rekol itself recorded in a hook command — bounded at 5s, in its own process
  group, killed as a group on timeout, and memoised per path.) Classification reads the shebang
  and, for a
  direct Python interpreter, that interpreter's `site-packages` — including following an editable
  install's `.pth` to its target, because `dist-info` proves a package was *installed*, not that it
  *resolves*. The real orphan was exactly that shape: `dist-info` present, `.pth` pointing at a
  deleted worktree.
  Only exact evidence authorises removal advice — a missing Python interpreter, an absolute-path
  `.pth` whose target has no `rekol/__init__.py`, or a
  `site-packages` that was read and does not resolve `rekol`. A non-Python or `env`-resolved
  shebang is **not classified** (the shipped shim is `#!/usr/bin/env bash`, and "bash has no
  site-packages" would have advised deleting a working shim), and a `site-packages` that cannot be
  located or read is **cannot-tell**, never "absent" — reading cannot-tell as absent is the same
  mistake as #218. A setuptools *finder* `.pth` and a relative-path `.pth` are both cannot-tell:
  the first resolves through an import hook that would have to be executed, the second against a
  `sys.path` base we are only guessing at. Both are healthy layouts that an earlier version
  reported as orphans with removal advice.
  Hook executability is now its own check that runs **unconditionally**. `unrunnable_hooks()` has
  existed since #159, but `doctor` only reached it from inside the install-drift check, which
  returns early with no manifest — so wired-but-dead hooks went unprobed on any machine without
  one, though probing them needs nothing from the manifest (#215).
  **Stated limit — this is detection, not prevention.** The PATH walk sees only the PATH of the
  process running `doctor`, so an entry present solely on a hook's PATH is invisible from there; a
  green line reports *resolvability*, not a proven hook path. The companion check verifies that the
  recorded fallback is **runnable**, and does not establish which executable a hook resolves — a
  hook prefers whatever `command -v rekol` finds in its own environment, and reaches the fallback
  only when that fails. So in the originally filed failure, where the orphan comes first on the
  hook's PATH, neither check fires. Preventing that requires preferring the recorded path, which is
  a separate change.
- **`uninstall.sh` now names `rekol` console scripts belonging to other interpreters (#203).**
  It only ever considered `${BIN_DIR}/rekol`, so a script installed through a different
  interpreter survived an uninstall completely. Reported rather than deleted, deliberately: those
  files belong to Python environments this installer never created, often need root, and may be
  an unrelated tool named `rekol`. Deleting another installation's files to finish our own
  uninstall is not a trade worth making silently — naming them exactly is. It also **never
  executes** anything it finds on PATH, including under `--dry-run`: a dry run that runs
  strangers' binaries is not a dry run. Detection is the shebang alone, so it names the
  definitive case (a Python interpreter that no longer exists) and refers the rest to
  `doctor`, which holds the full classifier.
- **`doctor` reports when `rekol --version` disagrees with the code being run (#203).**
  `cli.py` uses `@click.version_option(package_name="rekol")`, which reads the installed
  distribution's metadata. For an **editable** install that metadata is frozen at install time,
  so `--version` keeps reporting whatever was current when `pip install -e` last ran while the
  source tree moves on. Measured on a live machine: `--version` said 0.5.5 while the executing
  code was 0.6.1 — six patch releases apart, and that stale number was repeated in coordination
  notes for days. INFO rather than a problem, since a drifting editable install is the normal
  state of a development checkout.
- **Auto-resume's freeze classifier is pinned against the codes real freezes actually produce
  (#143).** Measured on a live journal (64 entries): `rate_limit` ×30, `server_error` ×26,
  `authentication_failed` ×5, `invalid_request` ×3. The classifier is **correct on all 64** —
  only `rate_limit` is treated as resumable — but `authentication_failed` had no test at all
  despite being the second most common non-resumable code, and the existing `server_error` test
  supplies prose that real payloads never carry (`error` holds a bare code, with no `message`
  key), so a regression depending on that prose would have slipped past it. The new test uses
  the real payload shape and was confirmed to fail when `authentication_failed` is added to
  `LIMIT_ERROR_TYPES`.
  Also measured, and relevant to the feature's design: of the 30 resumable freezes **only 2
  carry a parseable reset time**. The "reset-aware" path the issue is named for therefore fires
  on 2 of 30 real cases, with the fixed `FALLBACK_DELAY_MINUTES` branch handling the rest —
  recorded on the issue rather than changed here, since what to do about it is a design call.
- **`doctor` now reports a store holding both `REKOL.md` and `MEMORY.md`.** The
  `MEMORY.md` → `REKOL.md` rename shipped without a migration: the SessionStart hook resolves
  `REKOL.md` and falls back to `MEMORY.md`, so **the fallback was the entire migration strategy**
  — and it only fires when `REKOL.md` is absent. Where both exist, `MEMORY.md` is never read and
  anything living only there is silently unreachable.
  Found on a live machine (shipwright, 2026-10-08): both files present, contents diverged, neither
  a superset — `MEMORY.md` held the infra/topic pointers while `REKOL.md` held the always-on
  rules. A session read one and got a partial picture with nothing saying so. This is the fourth
  instance of the same half-finished rename, after #177 (`skill.md`), #188 (`NON_MEMORY_FILES`)
  and doctor's own exclusion prose.
  The check reports the byte sizes, names which file is actually read, and gives a different
  remedy for identical copies (remove the redundant one) than for diverged ones (diff, fold, then
  remove). Deliberately silent when only `MEMORY.md` exists — that is the un-migrated state the
  fallback handles correctly, and a check that fires on the healthy path gets ignored.

### Changed
- The marker is written **after** the legacy pointer is archived, not before, so a failed archive
  leaves nothing claiming the directory is done. The previous order's stated rationale was false:
  it claimed the reverse would let a re-run re-migrate already-archived content, but
  `discover_files_in_dir` iterates files only and never recurses, so the archive cannot be
  re-discovered. The order is now asserted by observing the calls — the earlier test compared
  mtimes, which a move preserves, so marker-first passed it too.

### Fixed
- **The unquoted-form repair refused paths it should have accepted, including a spaced home
  (#221, review + product).** The upgrade recognised an unquoted legacy command only when the
  recorded path was spelled out of `[A-Za-z0-9._-]`, so an install under `/opt/rekol@2/bin` or
  `/Users/John Smith/bin` was **left PATH-first** — the bug this step exists to fix, on a bin dir
  the installer will happily write, since `--bin-dir` takes any path and a spaced home is ordinary
  on macOS. Third time this predicate has been wrong, and each time because the character class
  described what someone had thought of rather than what the installer writes.
  So every character was **executed** inside the real substitution
  (`out="$(command -v <missing> || echo <path>)"`) in bash, zsh, and zsh with `EXTENDED_GLOB`:
  `@ + , = % :` and a **single** space come through unchanged and are now accepted; **two** spaces
  and a tab collapse to one space, so they are refused — the text we matched would not be the text
  that ran; `^` and `#` fail under `EXTENDED_GLOB` (`no matches found`) and nothing establishes
  which shell runs a hook command, so they stay refused; `*` fails under zsh and `)` mangles the
  substitution everywhere. The path must also end in the binary the installer records. A mid-path
  `~` measured inert and was left out anyway.
  The 37-row table is a test that runs the **real** `def` through `jq` rather than restating the
  pattern, and it is proved able to fail three ways: against the old class (11 rows), with the
  consecutive-space clause removed (exactly the doubled-space row), and against a regressed
  `install.sh`.
- **There is now exactly one answer to "which hook commands are rekol's" (#221/#239, product).**
  #221 and #239 were written in parallel and both needed that answer. #239 put it at the top level
  on purpose, so no step could find it unset; #221 kept a second copy inside the upgrade step, which
  **shadowed** the shared one for every line below it and was one entry short. Nothing broke on the
  day — the only consumer above the shadow had already read the shared list — but two copies of this
  predicate is exactly how #235 shipped, with the softener and the message describing it using
  different tests. The copy is deleted rather than synced, and the bats suite now fails if the list
  is defined more than once, if the one definition is indented into a step, or if any reader passes
  a list of its own. Proved able to fail: with a second definition spliced in it names both lines.
  The copy omitted `_hook session-confidence`, on the grounds that the memory loader carries it and
  Step 7 replaces that whole command. The first half is right and the conclusion was not. **Measured
  with both lists** against a pre-#203 loader paragraph and a standalone hook: the loader is
  untouched either way, because every form must match the whole command or its two ends and the
  loader starts with its own `: rekol:backend=` marker — but a **standalone** legacy
  `_hook session-confidence` hook is upgraded under the shared list and was left PATH-first under
  the copy. So the omission was not neutral: it left one shape of the #203 bug in place.
- **The upgrade left the UNQUOTED fallback form behind, on three handlers in a real file (#203/#221,
  QA).** `"$(command -v rekol || echo /abs/path/rekol)" _hook …` was not upgraded, while the same
  command with `echo '<path>'` quoted was. The quotes were the only difference, and the hooks left
  behind kept resolving PATH first — an orphaned `rekol` earlier on PATH running instead of the
  install's own, which is the entire bug #203 exists to fix.
  **Provenance — and the first answer here was wrong.** I reported that no release tag wrote this
  form, from reading `install.sh`'s SOURCE at each tag, where the invocation is quoted. That
  answered the wrong question: what matters is what the installer **writes**. QA doubted it,
  measured real upgrades, and was right.
  `run()` is `eval "$@"`. Releases insert a missing handler as `run "jq --arg cmd '${CMD}' …"`, and
  `${CMD}` itself contains `echo '<path>'` — so interpolated between apostrophes its own
  apostrophes close and reopen the quoting, and eval's quote removal drops them. Reproduced
  standalone: the variable holds `… echo '/p/rekol') …` and what lands on disk is
  `… echo /p/rekol) …`. So **v0.5.2 — the first tag with both an add-handler step and the quoted
  invocation — through v0.5.13 all write this form** whenever they add a missing handler. The
  snippet-merge sites pass a file (`--slurpfile`), so their text never meets the eval, which is why
  only the `--arg cmd` handlers are affected and why they are the later-added ones.
  This head already writes it correctly, by accident: `bfcd3a9` changed those five sites to pass the
  command **by variable name** (`--arg cmd "$REKOL_JQ_CMD"`) for an unrelated bash-5.2
  `patsub_replacement` fix, and a named expansion inside double quotes keeps the value verbatim.
  Incidental and untested is how a fix regresses, so all five sites now carry a note saying why the
  shape must stay, and two tests pin it: no hook command may be interpolated into a `run()` string,
  and the two call shapes are **executed** through `eval` to show one strips the quotes and the
  other does not.
  The first version of that source guard was **partial**, and Codex caught it: it keyed on the
  variable's NAME ending in `_CMD`, so it missed the fifth writer site —
  `--arg cmd '${REKOL_INVOCATION_QUOTED} review --nudge'`, the SessionEnd review-nudge handler,
  whose variable is not called `*_CMD` and carries a tail after the brace. It keys on the `--arg cmd`
  value being single-quoted and interpolated now, which is the shape that actually breaks, and its
  controls assert **both** release shapes match and that this head's shape and `#222`'s path values
  do not. Restoring either writer shape fails the guard.
  Five code comments claimed a `v0.5.5 -> HEAD upgrade test` pins this. **No such test exists** — I
  wrote one, it could not reproduce the bug, and I removed it; the five comments outlived it. They
  now point at the source guard and QA's real-upgrade run. A real tag-to-tag upgrade turned out not to be reproducible here — on a fresh
  `settings.json` the snippet merge adds those handlers first, so the add-handler step no-ops, and
  chaining real old installers needs a provisioned venv per tag. QA measured the real upgrades from
  the outside; rather than ship a test whose fixture does not reproduce the bug, the source check
  asserts the mechanism is absent. (`#222`'s other interpolated values — paths — stay out of this
  change.)
  The form is recognised now as a **literal absolute path only**, by #239's allowlist rule: unquoted
  means the shell word-splits and expands it, so `echo $(…)/rekol`, a path containing a space, and
  anything carrying an operator are **not** ours and are left byte-identical. The quoted form can
  afford to be permissive because the quotes protect it, which is why the two forms get different
  guards rather than one shared one.
- **`doctor` now reports a hook that resolves PATH before the install's own binary (#221).** A form
  the upgrade cannot reach must not also be silent: the upgrade only rewrites what it recognises,
  and QA found a form it did not. The new check is keyed on the **absence** of the current
  recorded-path-first rendering rather than on a list of legacy ones — the one direction that stays
  correct when a form nobody anticipated turns up, which is exactly what happened here.
  It is a separate finding from `hook executability`, which asks whether the recorded fallback is
  *runnable* and says explicitly that it does not check which executable a hook resolves. These two
  can disagree, and the disagreement is #203.
  A negative control caught a real defect in the first attempt: keying on the handler name alone
  reported `my-own-gate --strict _hook session-tasks` as a broken rekol hook, because it contains
  `_hook session-tasks`. Codex then found a second one — `printf '%s\n' '"$(command -v rekol || echo
  /p/rekol)" _hook session-tasks'` was reported, where the rekol text is an **argument being
  printed**, not an invocation. Doctor now reports only what the upgrade itself would own, by the
  same allowlist rule, **as the command itself** and anchored at its start; anything more exotic
  goes unreported, which is the safe way round for a check whose failure mode is telling a user
  their own command is broken.
  The OK line said "every wired hook tries the install's recorded binary before PATH", which claimed
  more than the check inspects: a PATH-first hook whose path the allowlist refuses — one containing a
  space, say — got that ✓. It now reads "every rekol hook **this check recognises** tries the
  install's recorded binary before PATH — a PATH-first command whose path is not one rekol writes is
  left alone and not inspected".
  A four-reviewer panel, standing in for Codex while its budget is exhausted, then found that the
  OK line was a **wrong green** on two real hooks: a PATH-first `review --nudge` (the SessionEnd
  handler) and a PATH-first `session-index --incremental` (the detached indexer) both reported OK,
  because the handler was matched as `_hook <name>` and neither is spelled that way. A green tick
  over a hook that prefers an orphaned `rekol` is the exact defect this check exists to report, so
  the owned **tail** is matched now rather than the sentence reworded. Kept local to this check:
  `unrunnable_hooks` shares the old regex and carries #241's own false positive.
  The same panel found the source guard could pass on a file it could not read (`grep … || true`
  swallows exit 2 as readily as exit 1) and had no control proving the scan reached the five writer
  sites. The second is now pinned and fails when a site disappears; on the first, honestly, this
  suite's `setup_file` already runs `install.sh`, so an absent or unreadable file fails before the
  guard runs — I tried both as controls and got `setup_file failed` rather than my own message, so
  those two lines are defence in depth and are labelled as such rather than as a fix.
  Two further ownership errors are filed rather than fixed (#254): `rekol search '_hook
  session-tasks'` is reported as a stale hook, because the handler is matched as a substring
  independently of the invocation; and an unquoted `${HOME}/bin/rekol` is accepted as ours when by
  #239's own rule an expansion should not be. Both make doctor report too much, never too little.
  One gap is recorded rather than left to be found: `_HANDLER_RE` matches `_hook <name>`, so the two
  owned hooks that are not `_hook` handlers — `session-index --incremental` and `review --nudge` —
  are not reported here even in a PATH-first form. Widening it would change `unrunnable_hooks`,
  which carries #241's own false positive and is out of scope. Both are still **upgraded**; only
  doctor's report about them is missed.
- **A recorded path containing `)` made `doctor` call a working hook broken (#203, Codex).** The
  parser bridged the `if [ -x … ]` preflight and `command -v rekol` with `[^)]*?`, which cannot
  cross a `)` — so for a path like `/Users/a)b/bin/rekol` the match failed, the parser returned
  `None`, and the report said "bare invocation … exits 127" about a hook that exits 0. It now
  consumes **single-quoted runs whole**, including the `'\''` splice, so a `)` inside quotes is part
  of the path while a bare `)` still ends the substitution. Verified against four path shapes
  (ordinary, `)`, apostrophe, spaces-and-`&`), and the `a)b` case is **executed**: the rendered
  command runs and returns the version, and `unrunnable_hooks` reports nothing — parsing it while
  the shell could not run it would be no better than the bug.
- **An ordinary v0.5.13 upgrade is free of duplicate handlers, measured on a real install (#236).**
  That issue was filed as no-hold because the duplicates had only been seen on a hand-edited
  `settings.json`. Measured properly: a **real v0.5.13 install**, upgraded twice with this branch
  plus #235's fix, ends with **exactly one of every handler on both runs**, no `command -v rekol`
  text left, and the memory loader marked and soft-failing. One of each: loader, `session-confidence`,
  `session-coverage`, `session-tasks`, `update-check`, `auto-reindex`, the SessionEnd notice,
  `session-index`, `review --nudge`, `time-context`, `capture-nudge`, `record-stop`.
  Pinned by a test built from the v0.5.13 *rendering* read out of that tag's snippets rather than a
  hand-edited file. Against the pre-rework matcher it fails on the first run with seven old-form
  invocations left — which is the state that makes a later run append a second handler.
- **The hook migration rewrote commands rekol does not own, and now matches exactly.** This step
  writes to the user's `settings.json`, and three successive versions of its matcher each rewrote
  something that was not rekol's: a `gsub` of the old invocation anywhere in a command string, then
  a shell-context guess, then an ownership regex that accepted any `_hook <name>`. Each was
  reviewed, shipped, and found wanting on a new shape. A pattern cannot express "we wrote this".
  The matcher is now an **equality**: a command is rewritten only when the WHOLE string equals a
  rendering the installer itself produces for a handler on an explicit allowlist
  (`REKOL_OWNED_HOOKS_JSON`), with the recorded path the only part free to differ — and that path
  is read by splitting on the literal text either side of it, so a path containing `)`, `&` or a
  space works, while one containing an apostrophe is refused because the renderer that wrote these
  never escaped it (that breakage is #222).
  Four shapes that a pattern got wrong are now controls in the suite: a `printf` whose text holds
  the old form and a real handler name, a command carrying an option rekol does not pass, a
  `_hook` name rekol does not own, and the user's own command. All four come back from this step
  untouched, and the recorded path `a)b` round-trips.
  Two legacy renderings are accepted, each with and without the soft-fail tail Step 6.96 only began
  appending in #183, because an install older than that stored the command without it and the
  presence checks would otherwise no-op on it forever.
  **The allowlist is drift-guarded three ways**, because a hand-maintained list that silently omits
  a handler reproduces the original bug: a bats test reads what a real install *wrote* and requires
  every rekol command in it to be an allowlist entry; a pytest requires every invocation shipped in
  a snippet to be on the list, excusing only one that appears solely inside the memory-loader
  command (which Step 7 replaces whole) and asserting that step still exists; and a third pins
  `rekol resume enable`'s own rendering — written by a different file entirely — to the same list.
  **The count of preserved occurrences is reported even when nothing was migrated.** That is the
  case that matters most, a `settings.json` holding only commands rekol does not own, and it is
  exactly the case the old report missed: it sat inside the "we changed something" branch, so its
  silence was indistinguishable from having nothing to say. It also counts occurrences rather than
  `grep -c` lines, which agreed only by accident.
  **Not yet byte-identical overall, and the installer still says so.** Step 6.96's soft-fail pass
  appends ` 2>/dev/null || true` to any command whose text merely mentions `rekol`, so a preserved
  command does not come back unchanged. An earlier version of this message claimed "rekol does not
  rewrite what it does not own", which the same install then falsified; printing a guarantee the
  next step breaks is worse than printing nothing, so it states only what this step does and names
  the gap (#235).
  **That gap is in released versions.** The soft-fail pass has matched on text since #183
  (2026-08-18), so it is present in v0.5.13 and on `main`: any hook of yours whose command mentions
  `rekol` — including a path such as `~/src/rekol-tools/gate.sh` — has had ` 2>/dev/null || true`
  appended on every install since. For a **blocking** hook that matters: `|| true` forces exit 0
  and `2>/dev/null` discards stderr, which is how Claude Code receives a block reason, so such a
  hook stops blocking. Your original is backed up as `settings.json.bak-softfail-*`. The fix is
  #235, landing in its own change.
- **`doctor` took an unrelated executable preflight for rekol's binary.** With an
  `if [ -x '/usr/bin/true' ]` earlier in the command, the fallback parser returned `/usr/bin/true`,
  probed it, found it runnable, and reported a hook whose real fallback does not exist as healthy —
  the decoy false-health class again through a different token. The matcher now requires both the
  `$(if [ -x …` opening of rekol's own rendering and the `command -v rekol` that only that
  substitution contains.
- **Re-running the installer did not actually upgrade existing installs (#203).** Step 6.95 rewrote
  only a bare `rekol <subcommand>`, never the previous `$(command -v rekol || echo '<path>')`
  rendering — which is the form every existing install carries. The handler-presence checks then
  saw a correctly named handler and no-op'd, so a reinstall left those hooks byte-for-byte
  PATH-first; only the memory loader was upgraded, which is what made this look fixed. Codex
  reproduced it against a fixture install.
  The old rendering is now migrated in place first, so the existing checks see already-correct
  commands. **Existing installs must re-run `./install.sh` to be upgraded** — the fix is in the
  installer, not in the running code, so nothing happens until it is re-run.
  Proven by a regression that downgrades a complete install back to the old rendering, re-runs the
  installer, and then **executes** the upgraded hooks with an orphan `rekol` first on PATH. With
  the migration removed it reports 7 hooks still PATH-first and names them.
- **`doctor` reported every newly rendered hook as broken.** `unrunnable_hooks()` parsed only
  `command -v … || echo …`, so the current `if [ -x … ]; then printf …` rendering yielded no
  fallback and each healthy handler was reported as "bare invocation with no absolute fallback —
  exits 127". Codex measured seven such findings against a real fixture install whose hooks
  executed fine. That is the exact false-positive shape this check exists to end: a working install
  reported broken. The parser now understands both renderings — the old one is kept, because
  installs predating the change carry it — and handles a path containing an apostrophe (arriving as
  the `'\''` splice), an ampersand, or spaces.
- The `#203` orphan regression now also asserts a **positive** trace that the recorded binary ran.
  "The orphan did not run" is satisfied when *nothing* runs, and every hook there is executed with
  errors suppressed, so the two outcomes were indistinguishable.
- Five `jq`-site comments blamed the 18 hook-test failures on quote interpolation through `eval`.
  That was disproved in a container — `eval` of this invocation returns 0 on bash 3.2 and 5.2
  alike — and the real cause was bash 5.2's `patsub_replacement` expanding an unescaped `&`.
  Passing the command by variable is still right, but for its own reasons, and the comments now say
  so.
- **One of the new guard tests only passed on a machine that had run Claude Code.** It asserted
  that a whitespace `CLAUDE_CONFIG_DIR` is reported as reaching `~/.claude/projects` — but the
  guard deliberately reports only destinations that EXIST, and a GitHub-hosted runner has no
  `~/.claude`, so the assertion failed there for a reason unrelated to the guard. (QA's image
  creates the real defaults on purpose, which is why its run was green.) The guard half now skips
  with that reason stated, and the half that was actually wrong — blank means *unset*, so the code
  resolves the real default while a raw-string check reads "pointing at nothing" — is pinned by a
  new test that needs nothing to exist and so runs everywhere.
- **A test could write into the developer's REAL memory home, and the suite's isolation was
  revertible by the tests it isolated (#224, Codex + product).** `conftest.py` redirected
  `REKOL_HOME`, the index cache and the archive using the **shared** function-scoped `monkeypatch`
  fixture — so a test calling `monkeypatch.undo()` to drop one `setattr` of its own also undid all
  of that. Thirteen tests in the suite call `undo()`. Codex's sandbox caught one of them then
  running the CLI, which tried to write `.partial-*.md.tmp` into `~/Dropbox/memory/knowledge/`; a
  local run has no sandbox. Measured with the fix reverted, such a test leaks
  `REKOL_HOME=~/Dropbox/memory`, `MEMORY_HOME`, `REKOL_INDEX_DIR=~/.cache/rekol/<hash>` and
  `REKOL_ARCHIVE_DIR=~/.local/share/rekol/archive`.
  Fixed as a class rather than per test, because converting thirteen call sites leaves the
  fourteenth free to do it again: **isolation must not be revertible by the code it isolates.**
  `conftest` now holds its patches in a private `pytest.MonkeyPatch` undone at teardown, and a
  `pytest_runtest_call` wrapper **fails** any test left pointing a steering var outside tmp.
  A four-reviewer panel then found three more, two of them in the tests meant to pin all this:
  the **undo test was vacuous in CI** — it asserted the variables were ABSENT after `undo()`, which
  is also what a reverted fixture produces when nothing was exported, so it caught the regression on
  a developer laptop and never on a clean runner; it now asserts the variables conftest SETS are
  still set and still under tmp, and the reviewer's mutant fails in a clean environment. The **hook
  wrapper was never exercised** — every test called the helper directly, so disabling the raise
  passed 5/5; two subprocess tests now run an inner pytest session and pin both the raise and the
  gate that stops it masking a test's own failure. And two tests **broke when `HOME` is inside a
  temp dir**, which is precisely the sandbox shape this guard exists for, so the "real directory"
  they need is now found rather than assumed to be `~`.
  Also corrected: the comment claiming the resolution-time check was "filed as a follow-up" when it
  was not — it is **#255** now — and the claim that the variable list covers *every* steering
  variable, which it does not (`CLAUDE_SETTINGS_PATH` is read raw by `cli_resume.py` and is not
  covered; `REKOL_TOOLS_HOME` is listed and read only by `install.sh`).
  **The guard asks `config.py` where it would GO**, rather than re-deriving the expansion itself —
  because re-deriving was wrong twice. Codex caught `REKOL_INDEX_DIR='~'`, whose raw string neither
  exists nor sits under tmp. A review then found the same class again: a set-but-**empty** or
  whitespace value, which `config.py` treats as unset and resolves to its REAL default while the env
  scan skipped it for being falsy. Measured, with a tmp `REKOL_HOME`: `XDG_CACHE_HOME=""` and
  `REKOL_INDEX_DIR=""` both land in `~/.cache/rekol/<hash>`, `XDG_DATA_HOME=""` in
  `~/.local/share/rekol/archive`, and `CLAUDE_CONFIG_DIR="   "` in `~/.claude/projects` — all four
  silent. The guard now checks the four destinations `load_config()` resolves (it is read-only, so
  it cannot create what it warns about) and keeps the env scan as a second layer for when no home is
  configured. One of my own tests asserted the whitespace case was a harmless sentinel; it was not,
  and it now asserts the opposite.
  Also fixed: a set-but-empty `TMPDIR` made `Path("")` resolve to the CWD, so the repo root counted
  as a temp root and everything inside it was exempt. `tempfile.gettempdir()` ignores an empty
  TMPDIR. And the isolating fixture's `try` now opens before its first patch, so a failure
  mid-setup cannot leave patches applied with no teardown.
  **The guard resolves each value the way the code does** before testing it, which is the second
  thing it got wrong: `REKOL_INDEX_DIR='~'` was skipped twice over — the literal `~` neither
  resolves under tmp nor exists — while `config.py` expands it with `os.path.expanduser` and writes
  to the real home (Codex). A guard that checks a different string from the one the code uses is
  checking nothing. Its report now names the expansion too, so it says which directory was at risk
  rather than the string that looked harmless.
  **And it claims only what it does:** a post-test check of configured destinations, not a write
  barrier. It reports where a test was POINTED, so a test that writes outside tmp and then restores
  the variable is invisible to it — notably one using `pytest.MonkeyPatch.context()`. Checking at
  resolution time is filed as a follow-up.
  The guard is a hook wrapper, not a fixture, and that mattered: as a fixture it caught the
  undone-isolation case but **not** a test that set a real path deliberately, because the test's own
  `monkeypatch` is torn down first and the env was already restored before it looked. It also keys
  on the path **existing**, after firing on seven healthy tests that set `/a`, `/b` and whitespace
  as deliberate resolution-order sentinels — a guard that fires on those is one somebody deletes.
  On its first real run it found a pre-existing gap of its own: `REKOL_TOOLS_HOME` was never
  cleared, so on any machine with rekol installed every test ran with it pointing at the real
  `~/.local/share/rekol`.
- **The archive's audit paragraph claimed a narrower accepted window than the code has (#224,
  Codex).** It said the only accepted window was the interval between the last verification and the
  unlink. That holds only up to the moment the final hash **opens** the file. The read is
  sequential, so an in-place write landing **behind** the read position never reaches the digest at
  all — and with size, inode and mtime restored, every observation agrees while the digest is of a
  mixture no longer on disk. On the copy path **the edit is lost**.
  Product accepts the behaviour — it takes an in-place rewrite mid-hash with the mtime put back,
  which editors do not do, and closing it needs a second full read or a lock in the most-reviewed
  function in this repo. What was not acceptable was the claim. Both limits are now stated, and the
  hashing helper's docstring no longer says the opposite of what was measured.
- **An archive entry was deleted on a guess, and the verification it guarded was optional (#224,
  Codex + product).** Five faults in the refusal handling, all reproduced before being fixed.
  **An archive entry was removed while it held the last copy of those bytes.** The hard-link path's
  mismatch branch unlinked the archive entry on the strength of a comment — "it is a hard link to
  the still-present source" — that stopped being true one line earlier: replacing the source
  pathname between the inode check and the final hash leaves the archive entry at link count 1. The
  unlink destroyed those bytes and the import undo destroyed the only other copy of them. The
  removal now has the precondition the argument assumed: the entry is the same inode as the live
  source **and** the link count shows another name. Otherwise it is kept and the message says which.
  On the copy path that precondition can never hold — a verified copy is a different inode by
  construction — so a copy-path entry is now always kept, reversing the previous round's "remove it
  so a retry finds no twin". The cost is an untidy archive after a refusal, which nothing searches;
  the alternative was destroying bytes on a guess inside a data-loss fix.
  **Verification was optional on the paths that needed it.** A failed initial hash was logged and
  the run carried on with no expected digest — turning the import/retire check off for the one file
  nothing could vouch for, which left old bytes in the home and retired the new ones. It now fails
  closed: no import, no archive, source untouched, directory not retired. The **duplicate** branch
  retired without a digest at all, which was the one call site whose entire claim is "these bytes
  are already in the home"; Codex reached old-in-home / new-archived **with no errors** through it.
  It passes the digest now.
  **The byte-identity guard on the import undo was a text comparison.** `read_text` normalises
  newlines, so a file rewritten from `hello\n` to `hello\r\n` read back identical and the guard
  deleted an edited file. It compares bytes, and the import is written as UTF-8 with no newline
  translation so the comparison can be exact.
  **A source-unlink `PermissionError` bypassed the cleanup**, arriving as a bare `OSError`, missing
  the refusal branch, and leaving the import in the home under a message that never mentioned it.
  Both publication paths now report that case as a refusal, and the migrator undoes the import on
  **any** failure to retire rather than only on its own exception class.
  **An interrupted-then-edited retry** keeps both versions in the home — product ruled that
  building provenance to reconcile them is not worth it here, since it needs a kill, then a user
  edit, then a retry, and nothing is lost — but the run is no longer silent about it: it names the
  file it wrote, says both are there and that nothing was deleted. Filed for reconciliation.
- **A migration could leave a STALE memory in the home while the newest text went only to the
  archive (#224, QA + product).** Measured on the **ordinary hard-link path**, which is why this held
  the tag: the import read the old bytes, the user edited the file in place, `os.link` published the
  inode — now carrying the *new* bytes — the source was unlinked and the directory retired. **Exit
  0, nothing reported.** The newest text survived only in `old-memory-archive/`, which search never
  reads, and the home held an ordinary-looking memory with the old content.
  The inode check added earlier passes here, because an in-place write keeps the inode: it proves
  the file is the same *file* and says nothing about its *bytes*.
  **The contract now is that the home only ever holds the bytes that were retired.** Each source is
  hashed *before* classification reads it, and that digest is handed to the archive, which refuses
  unless the bytes it is about to retire are still those bytes. The check sits immediately before the
  unlink on **both** publication paths, so the only remaining interval is the last-check-to-unlink
  one already accepted — not a wider one.
  **A refused retirement now leaves no import.** The import this run wrote is removed, guarded:
  only a file this run created, and only while it still holds exactly the bytes this run wrote. If
  an undo cannot happen, or the file is no longer ours to touch, the message **names the file and
  says it is still there** rather than claiming a removal.
  The **archive entry** is a separate question, and the answer is narrower than the first version of
  this fix assumed: see "an archive entry was deleted on a guess" below.
  This also closes what QA measured on the copy path: a retry used to leave `p-note.md` (old) beside
  `p-note-1.md` (new) — same name, same dates, nothing to tell them apart, and search returning both
  as equals with the stale one first. A retry after a **refusal** now leaves one memory holding the
  current content. (A retry after an *interruption* that the user then edited keeps both versions
  and says so — see below.)
  Tested on both paths with QA's walnut/copper fixture, plus a kill between the import and the
  archive ending whole after a retry. Each part is revert-proved and fails on its own rows.
- **An atomic save during the final verification hash was silently deleted (#224, Codex).** Hashing
  reads through an open descriptor, so an editor replacing the pathname mid-read left the descriptor
  returning the OLD inode's bytes to EOF. The comparison against the archived copy then *succeeded*
  and the unlink deleted the NEW file. Reproduced by Codex with a 3 MiB source replaced after the
  first 1 MiB: publication reported success, the source was gone, the archive held the old content,
  and the new content existed **nowhere**. That is a whole-hash window, not the brief post-check
  interval that was accepted.
  A digest is now taken together with proof of which file it is a digest of: `fstat` on the
  descriptor before reading, `fstat` on it again after, and `os.stat` on the pathname — device,
  inode, size and mtime must all agree. On any mismatch, or any failure to establish them, the
  source is kept and the directory is not retired.
  **What remains accepted, in one sentence for both paths:** a write of any kind in the interval
  between the last verification and the unlink. Everything earlier is caught. The docstring no
  longer says "no content window at all" for the hard-link path, which was too strong — it is not
  an atomic compare-and-unlink. Two narrower wordings that were in this file and in the code are
  gone, both wrong: "accepted: an in-place write leaving size, mtime **and content** unchanged"
  describes nothing at all, and "leaving size and mtime unchanged" is a limit on what the *identity*
  evidence can see, not something this function accepts — the digest comparison catches those bytes
  regardless. That limit is now documented where it lives, on the hashing helper.
- **Archiving deleted new bytes when an editor saved over the source.** The hard-link path's
  guarantee — same inode, so removing the source name cannot lose content — holds only while the
  pathname still *denotes* that inode. An editor's atomic save replaces the pathname with a brand
  new file, so the unconditional unlink removed the **new** bytes while the archive kept the old
  ones. The source's device and inode are now confirmed against what was just published,
  immediately before the name is removed; on a mismatch both files are kept, the run reports it,
  and the directory is not retired. The published copy is deliberately *not* removed — it is the
  only name left for the bytes that were there, so unlinking it would destroy them.
- **The no-hard-link fallback wrote bytes under a final name, and imports inherited it.** It copied
  the whole file into the final name, so an interruption left a valid-frontmatter fragment at
  `knowledge/<name>.md`, directly visible to the indexer — QA's wrong-memory hazard, reached
  through a different door. The fallback now claims the name with an **empty placeholder** and then
  `os.replace`s the staged file over it, so a final name only ever holds the placeholder or the
  complete file. The worst interruption leaves a 0-byte file, and a test asserts that is inert by
  running the real indexer over it: zero files indexed, zero chunks.
- **Verification ran before a copy that could still lose data.** On the fallback the digest check
  preceded a second full copy, so a source mutated during that copy was lost with the check already
  behind us. The source is now re-verified **after** publication, covering everything up to the
  instant before its name is removed. A separate completeness check stays *before* publication, so
  a short stage is never published — collapsing those two into one check is a hole this fix briefly
  opened, and a test caught it.
- `archive_copy_looks_complete()` is removed. It had no production caller, so it implied a recovery
  contract nothing enforced; with placeholder-then-replace there is no partial file under a final
  name for it to detect.
- **A fault mid-import left a truncated memory under an ordinary name.** The import reserved the
  target name with `O_CREAT|O_EXCL` and then wrote into it, so a write that failed part-way — a full
  disk, a hard kill — left a fragment that *looks* like a memory. QA measured the consequence: the
  fragment keeps its frontmatter, so it parses, the indexer indexes it, and in their fixtures search
  ranked the fragment **above** the whole copy. Not an absent memory but a wrong one, on an ordinary
  path.
  Imported files are now staged and published the same way the archive and the marker are: written
  to a temporary the indexer cannot see (leading dot *and* a `.tmp` suffix the `**/*.md` walk cannot
  match), fsynced, then published by an atomic no-clobber claim. A fault leaves nothing under an
  ordinary name and nothing that could be mistaken for a memory, and a retry imports exactly one
  whole file. The cleanup catches `BaseException`, so a `KeyboardInterrupt` mid-write does not leave
  the staging file either.
  Suffixing for a name collision now matches the archive's convention (`-1`, `-2`, …) rather than
  starting at `-2`, because both paths share one publication primitive.
- **The no-clobber archiving fix introduced a data-loss window, and this closes it.** Copying the
  file and then unconditionally unlinking the source loses an edit made in between: the source is
  gone and the archive holds the old bytes. The `rename` it replaced had no such window, so this
  was a regression on ordinary filesystems.
  Publication is now by **hard link** wherever hard links work, which is the ordinary case:
  `os.link` is atomic, fails on an occupied name, and publishes *the same inode*, so removing the
  source name afterwards cannot lose bytes — there are no second bytes to go stale. Copying is no
  longer the ordinary path.
  Where a real copy is unavoidable — a symlink source, or a filesystem without hard links — the
  source is re-read and re-hashed after the copy and **before anything is removed**. On any
  mismatch the source is kept, the failure is reported, and the directory is not retired. A digest
  rather than a size, because a same-length edit is exactly what a size check waves through.
  The concurrent-writer contract, stated rather than implied: on the copy path a write landing
  after the re-verification and before the unlink is still lost; on the hard-link path no such
  window exists.
- **A dangling or looping `MEMORY.md` was mishandled at the migration entrypoint.** `exists()`
  follows the link, so a dangling one looked absent and the directory was retired with no errors at
  all — the link's content silently abandoned. A self-loop raised an uncaught `ELOOP` out of
  `is_retirement_pointer`, before anything could report it. Symlink entries are now inspected
  without being followed, an unreadable target is reported by name, and the directory is not
  retired when preservation failed. Tested through `migrate_dir`, not only the publication helper.
- `archive_copy_looks_complete()` exists so a later run does not assume a present archive name is a
  good copy: the fallback path can be interrupted mid-write under a real name. Nothing describes
  that case as complete.
- **Archiving could overwrite the only byte-complete copy of a file.** `_free_archive_name` tested
  `exists()` and then called `shutil.move`, which replaces its destination — so a file created at
  that name in between was destroyed. Codex reproduced the loss of an earlier archived original
  with a deterministic probe; sequential suffix tests cannot see it. Two more holes in the same
  code: `exists()` is False for a **dangling symlink**, so such a name read as free, and across
  filesystems `shutil.move` becomes copy-then-unlink, where a failed copy leaves partial debris
  that the next run's `exists()` would treat as an occupied, complete archive copy.
  Publication is now staged: the content is copied into a unique `.partial-` temporary, fsynced,
  and the final name claimed with `os.link`, which is atomic and **fails** if the name is taken —
  a dangling symlink included, since the check is on the name and not on what it points at. A real
  archive name therefore only ever appears as a complete file, and the source is unlinked only
  afterwards. A short copy is refused rather than published.
- **A relative `MEMORY.md` symlink broke when archived**, making "kept byte-complete in the
  archive" untrue: moving the link re-resolved its target against the archive directory. Symlink
  sources are now followed and archived as real content, which makes the claim literally true,
  while the target file is left exactly where it is — only the link is removed, which is all that
  is needed to stop autoMemory injecting it. A **dangling** source symlink has no content to
  preserve, so it is refused and nothing is touched.
- **A partially written `.MIGRATED` marker was accepted as a completed migration.**
  `has_migration_marker()` only asks whether the file exists, so a short write produced a
  directory that reported itself migrated while the same run reported "NOT retired, so re-running
  retries it" — and the next run believes the file, not the report. The marker is now staged and
  renamed into place, so it either does not exist or is complete.
- **Importing a file could overwrite an existing memory, or write through a dangling symlink.**
  The target write picked a name with `while candidate.exists()` and then called `write_text`:
  two migrations at once could pick the same free name and the second overwrite the first's
  import, and a dangling symlink at the candidate name read as absent, so `write_text` followed it
  and created or truncated whatever it pointed at, anywhere on disk. Both are closed by a single
  `O_CREAT|O_EXCL` create.
- **The dry-run preview named a path `--commit` would not use.** It hardcoded
  `old-memory-archive/MEMORY.md` while a commit would pick `MEMORY-1.md` when the first name was
  taken. The preview now predicts the same name the publication would choose.
- **The nested-file warning could announce "this directory is now retired" in the same report as
  the error saying it was not.** It was worded before the retirement was attempted. It is now
  worded after, and on failure says the directory was *not* retired, a later run will retry it, and
  no marker needs deleting because none was written.
- **`rekol migrate` permanently deleted a real `MEMORY.md`, with no copy anywhere (#224).** Found
  by QA against `508b33f` and present in published **v0.5.13**. `write_retirement_pointer` called
  `legacy_pointer.unlink()`, and because `discover_files_in_dir` deliberately skips the name
  `MEMORY.md`, the file was neither imported nor archived first — after
  `rekol migrate auto --commit`, content that lived only there existed **nowhere on disk**. A
  directory holding only `MEMORY.md` was emptied and marked migrated, exit 0.
  The unlink could never do the job it was written for. Its stated purpose was removing a
  *tombstone* left by an older version of this tool, but both callers sit behind
  `has_migration_marker`, which already treats an old-format tombstone as retired — so a real
  tombstone never reaches it. **Everything it could delete was a user's own file.** And it was
  pinned by a test that wrote `"Real content."` and asserted the file was gone, so this was a
  decision carried from tombstones to real files, not a slip.
  `MEMORY.md` is now **moved into `old-memory-archive/`**, never unlinked. That still stops
  Claude Code's autoMemory injecting it — autoMemory cannot inject what is not at that path — and
  it is the same guarantee `install.sh` already printed ("originals stay in place or move to
  `old-memory-archive/`") and `archive.py`'s header already claimed ("the only byte-complete
  copy of each original"). Both were false; now they are true.
  An earlier archived copy is never overwritten (`MEMORY-1.md`, `MEMORY-2.md`, …), because the
  archive holds the only original and one silent overwrite is one unrecoverable file.
  **The move is reported in dry-run and in the real run, and deliberately not gated on
  `--quiet`** — `install.sh --migrate` passes `--quiet`, and QA measured the deletion producing
  no output at all on that path, so the one destructive step was also the most silent. Dry-run
  previously said "would migrate N" and never mentioned `MEMORY.md`, so the only destructive step
  was the only one a user could not preview.
  Reach: `migrate auto --commit`, `migrate repo <path> --commit`, and `install.sh --migrate` —
  which is the flag the README advertises for importing auto-memory, so the users who hit this
  were the ones trying to keep theirs. A plain `install.sh` was unaffected.
- **A retired directory could abandon files in subdirectories without saying so (#225).**
  Discovery is deliberately non-recursive, but retirement is permanent: a nested `.md` was left on
  disk, never imported, never archived, and no later run looks — while the marker claimed the
  originals were in `old-memory-archive/`. Migration now names every such file and says plainly
  that they are neither imported nor archived, and the marker says "files discovered here"
  rather than implying everything. The retirement invariant held only because discovery defined
  what counted.
- **Dry-run previewed the `MEMORY.md` move in only one shape (G1).** `progressed` keyed on
  `migrated > 0`, which stays 0 in dry-run by design, so in the commonest shape of all — a
  `MEMORY.md` beside one topic file — nothing was previewed and the nested-file warning never
  appeared either. It looked correct only for a directory with no topic files, which takes a
  different early return that has its own preview; the test covering that shape passed while the
  ordinary path previewed nothing, so the one destructive step in the run was the one step a user
  could not see coming. Dry-run and `--commit` are now asserted to agree about the destination.
- **A directory whose only `.md` files were nested retired in silence (G2).** The warning sat below
  the `if not files:` return, so those directories reported `migrated 0`, exit 0, and never
  mentioned the files left behind — the summary line looked like there had been nothing to do.
  These are the likeliest shapes to be overlooked, and they are now warned about in all four
  combinations of `MEMORY.md`/topic file/nested file.
- **The nested-file warning's remedy did nothing when followed (G3).** It said to move the files up
  one level and re-run; QA did exactly that and nothing happened, because retired directories are
  filtered out of discovery. It now names `.MIGRATED` as well, and states the files are not
  imported and not searchable. There is a regression test that *executes* the remedy rather than
  reading it. In dry-run the wording also no longer claims "this directory is now retired" — a dry
  run retires nothing, so the past tense was the same species of false claim the warning exists to
  prevent, and it advises moving the files before committing, where no marker needs deleting at all.
- **An unwritable archive left the directory permanently retired (G4).** `shutil.move` had no
  `except`, so a mode-555 `old-memory-archive/` produced an uncaught `PermissionError` traceback —
  and because the marker was written *first*, `.MIGRATED` claimed completion with `MEMORY.md` still
  in place, so `has_migration_marker` skipped the directory for good and the file was stranded.
  The legacy pointer is now archived **first** and the marker written **last**, the failure is
  caught and reported as a plain error, and nothing claims the directory is done — so a later run
  retries once the archive is writable.
  The previous order's stated rationale was also wrong and is corrected in the docstring: it
  claimed marker-last "would leave neither file on partial failure, causing a re-run to re-migrate
  already-archived content", but `discover_files_in_dir` iterates files only and never recurses, so
  the archive cannot be re-discovered and that failure mode does not exist.
- **`migrate repo <repo root> --commit` moved a repository's tracked files.** QA pointed it at a
  real git repo and `README.md` and `CLAUDE.md` were moved into `old-memory-archive/`, leaving them
  deleted in the work tree. Losing `CLAUDE.md` also takes a project's instructions out of Claude
  Code's view. The reasoning this release uses for sparing `MEMORY.md` — likely a tracked project
  file, and an untracked `old-memory-archive/` appearing inside someone's repository — applies
  verbatim to its siblings, so the contract could not be stated truthfully while this stood.
  `--commit` now refuses at a git work-tree root (exit 2) and names the memory subdirectory to
  point at instead. It is a refusal rather than a prohibition: a git-tracked notes repo is a
  legitimate migration source, and rekol's own `git_track` makes `REKOL_HOME` a work-tree root, so
  `--allow-repo-root` opts in. `.git` as a file counts too, so a worktree or submodule is covered.
- **Dry-run now names every file it would move**, for any path. It previously printed only a count
  (`would migrate 2 (defaulted=2)`), so previewing could not reveal which files were about to be
  moved — the preview was exactly the tool a user would reach for, and it withheld the one detail
  that mattered.
- The `moved MEMORY.md` and `MEMORY.md WOULD be moved` lines now also say **not searchable**, which
  the left-in-place line already said. A user told only that a file was "not imported" has not been
  told that searching will not find it.
- The `.MIGRATED` marker text no longer wraps mid-clause (it read `KEEP them unless you` / `have` /
  `checked…`), with a test that no line is a single orphaned word.
- **The alias fix moved the bug one level down, and a broken layer link made doctor say "cannot
  tell" (#232, review).** Three disagreements with the indexer, all found by a reviewer told to go
  looking for one:
  1. **`projects/a/topics -> projects/a/notes`** (#253): the `projects/` entry point was listed
     before the per-slug layers, so its walk met the file as `projects/a/notes/s.md` — correctly
     outside the layers under *that* name — and claimed the inode, pruning the `topics` walk.
     Doctor said OUTSIDE_THE_LAYERS while the indexer read the file, and advised moving files that
     were already indexed. Entry points are ordered **most-specific-first** now: whichever one makes
     a path indexable claims the inode before one that makes it outside.
  2. **A dangling `topics` link beside a real `always/s.md`** reported UNKNOWN, because `stat`
     raises ENOENT on it and that aborted the whole check. `Path.is_dir()` returns False for it
     silently, so absent is the answer that agrees with the indexer.
  3. **`topics -> topics`** needed a second fix after the first: the pre-check stopped forcing
     UNKNOWN, and then the WALK still did, because `os.walk` on a self-looping entry point raises
     ELOOP into `onerror`.
  An unreadable layer still reports UNKNOWN, which is correct — the difference is between "I cannot
  read this" and "this is not there". Three rows added to the agreement table, each failing on its
  own when its fix is reverted. Verified no regression on a 3-link symlink ring, a cycle inside a
  layer, a relative alias, and a deep alias target.
- **An alias inside the memory home read as outside the layers (#232, QA).** With
  `home/topics -> home/projects/shared` — one inode, two names — doctor met the file by its
  `projects/shared/...` name first, classified it correctly as outside the layers under *that* name,
  and marked the inode visited; the `topics` entry-point walk was then pruned and the file was never
  counted as indexable. So doctor reported OUTSIDE_THE_LAYERS while the indexer reached it through
  `topics`, and the advice it gave was to move files that were already indexed.
  Fixed by ordering: the entry points are walked **before** the root, so when one inode has two
  names the name the indexer resolves claims it first. Added to the agreement table, and
  revert-proved — restoring the old order fails that row alone.
- **`doctor` disagreed with the indexer in three more places, all the same fault (#232, Codex).**
  Each was its own hat on "doctor's idea of what is indexed differs from the indexer's", so they are
  fixed together and checked by one **agreement table** — a set of shapes run through both the
  doctor state and the real `_iter_memory_files`, required to coincide.
  1. **A symlinked top-level layer read as EMPTY.** `os.walk` does not follow links by default,
     while the indexer reaches a symlinked `topics/` through `Path.glob` — so a home whose memory is
     sitting right there reported "nothing to index" on an unbuilt index. That is memory silently
     dark, the worst direction to be wrong in.
     The first fix for it, `followlinks=True`, then overshot in the other direction, which looks
     harmless until you read the remedy (Codex + product, 2026-11-07): it also follows a link
     **nested inside** a layer, which `Path.glob("**/*.md")` never does. With only
     `topics/linked-subdir -> external/`, doctor said SOME and the indexer reached nothing — so the
     `rekol index rebuild` it advised could not work, and the user was told their memory was there
     while every rebuild kept producing an empty index.
     Links are now followed at exactly the names the indexer resolves itself — each layer dir,
     `projects/`, `projects/<slug>/` and `projects/<slug>/<layer>/` — and nowhere below them, which
     is the indexer's rule read off its own walk. Cycles are still guarded by `(device, inode)`, so
     a `topics/loop -> topics` symlink cannot hang it, and that guard is also what makes one walk
     per entry point count each directory once.
     **This is agreement, not reach:** a memory reachable only through a nested link is still never
     indexed and nothing says so, which is filed as #247 rather than papered over by letting doctor
     disagree with the indexer.
  2. **A home holding only a root `.draft.md`** reported it as unreachable memory. A hidden file at
     the root is not something the user is waiting to see in search, and `iter_all_markdown`
     excludes hidden paths, so it is neutral. Hidden files **inside** a layer are still counted as
     indexable, because the indexer's globs really do match them — the two rules disagree on
     purpose, and the table pins both.
  3. **A home whose `MEMORY.md` is a dangling pointer** read UNKNOWN. The traversal probe stat'd
     `next(iterdir(), None)` — whichever name the filesystem returned first — so a broken pointer
     made the whole check report "a missing answer" about a perfectly readable home. It probes only
     the entries the answer depends on: the layer dirs and `projects/`.
  An unreadable layer dir still yields UNKNOWN, and an unreadable `tasks/` still does not, so
  neither fix swallowed the other. Each of the three is revert-proved and fails on its own row.
- **The "is there anything to index?" count used a different scope from the indexer (#232,
  Codex).** It counted every non-hidden, non-excluded `.md` anywhere under the memory home; the
  indexer walks the layer dirs (`always`, `when`, `topics`, `knowledge`, `feedback`) and
  `projects/`. So a home holding only a stray `note.md` at its root reported "1 curated memory
  file(s) exist, so searching curated memory returns nothing" and offered `rekol index rebuild` —
  which would have built an **empty** index and changed nothing. Both the claim and the remedy were
  wrong.
  The scope question is now answered with the indexer's own scope, and the case gets its own
  answer rather than being rounded to one of the existing two: the files are real, no rebuild will
  reach them, and the remedy says where they have to live. The predicate is **pinned to the real
  walk by a property test** over a tree holding every shape the walk distinguishes — layer dirs,
  nesting, hidden names, flat and nested `projects/`, `tasks/`, strays, and the shipped pointer
  files — because a predicate that merely looks right is how #157/#158 happened. (It is a
  predicate rather than the walk itself because this check needs `os.walk`'s `onerror` to see an
  unreadable directory, which `glob` swallows.)
- **An unreadable `tasks/` made the whole answer UNKNOWN (#232, Codex).** `tasks/` is the
  machine-managed task layer, excluded from memory by design — so whether it is readable has no
  bearing on whether there is curated memory to index. It was still traversed, so an unreadable one
  tripped `onerror` and turned a perfectly healthy home into "a missing answer". Excluded subtrees
  are pruned before descending now. The counterpart is pinned too: an unreadable *layer* dir still
  yields UNKNOWN, so pruning cannot quietly swallow the finding #232 added.
- **`doctor` called an unbuilt curated index a problem even when there was nothing to index
  (#232).** On a machine with no curated memory yet, `✗ curated index: not built yet` was reported
  with the remedy `rekol index rebuild` — which would have built an *empty* index. Running it did
  not make the statement true, it only silenced the line, which is the tell that the severity was
  wrong. That is `doctor` red out of the box for the third time, after #165 and #231, and each time
  it teaches people to stop reading the report.
  "Not built yet" now has three meanings, matching what the checks either side already do (the
  archive check prints a neutral line for the same condition, and the session index only fires when
  transcripts exist):
  no curated files → **neutral**, no remedy; curated files present → **PROBLEM**, naming how many
  exist and that search returns nothing (the #165 bar, unchanged); the memory home unreadable →
  **its own PROBLEM**, reported as UNKNOWN, because "cannot tell" is not "nothing to index".
- **The file count silently undercounted an unreadable subdirectory.** `Path.glob` swallows a
  `PermissionError` on a subdirectory and simply yields nothing for it, so a readable memory home
  with one unreadable layer dir counted zero files and reported "there is no curated memory to
  index" — identical output to genuinely having none, with a real file sitting there. The walk now
  uses `os.walk(onerror=…)` so the failure is visible and reported as UNKNOWN.
- **Four ways a BROKEN install reported "first use" (#165/#231, review).** This PR exists to stop
  `doctor` reporting a fresh install as DEGRADED, and a reviewer sent to look for the opposite error
  found it four times — neutral on an install that can never index anything, which is the worse
  direction.
  **"Absent" turned out to be three different things**, because `os.stat` follows links and collapses
  them into one ENOENT/ENOTDIR: the name genuinely missing (a real first run), the name existing as a
  **dangling symlink** (the shape a relocated `~/.claude` leaves when its target moves), and a path
  **component being a regular file** (nothing can `mkdir` under a file). The last two were reported
  as "Claude Code creates it on first use" with no remedy. `os.lstat` separates them, and the same
  split fixes a dangling `sessions.db` link, which read as "not built yet" and offered
  `rekol session-index --full` — a remedy that writes through the same broken link.
  **A partial lockout of the transcript tree was silent.** The traversal probe stat'd
  `next(iterdir(), None)` — a single sample — so an unreadable slug went unreported whenever it was
  not the entry the filesystem happened to return first. Measured: four slugs with one at mode 000
  produced **no finding**, while `Path.glob("**/*.jsonl")` — what `sessions/ingest.py` walks —
  returned 3 of 4 transcripts. A whole project's history missing from the index with nothing said.
  Every entry is probed now, and `stat` alone was not enough either: stat'ing a mode-000 *directory*
  succeeds, since the search bit that governs it belongs to the parent. It asks `os.access` whether
  each slug can be read and traversed — 44 ms at 2400 slugs, measured.
  This is the sibling of the single-sample fault fixed on the curated side the same day; fixing one
  and not the other is how it survived.
  **And the visible check needed the same fix, not just the helper (QA).** For
  `sessions.db -> somewhere/gone` the `session index` finding a user reads still said "not built
  yet" and offered `rekol session-index --full` — a remedy QA measured **crashing**: exit 1 with
  `sqlite3.OperationalError: unable to open database file`. (With the link target's parent present
  SQLite creates the file through the link instead, so whether it crashes depends on the shape;
  neither case makes "not built yet" true.) It reports ✗ now, names the broken link, and the remedy
  is to remove or repoint it. My test for the first half asserted on `_prior_index_state()`, a
  helper, rather than on what `doctor` prints — the same mistake a review caught in #224 hours
  earlier, so this one goes through `run_doctor` and reads the finding.
- **An unreadable sessions DB read as a fresh machine.** The prior-index check returned 0 for a
  corrupt or unreadable DB, which is indistinguishable from "nothing was ever indexed" — so the one
  case where `doctor` genuinely cannot tell was reported as the reassuring one. Absent, empty,
  populated and **unknown** are now four distinct states, unknown is reported as unknown, and the
  store is closed on every path. There is an explicit control for an initialised-but-empty DB,
  which the neutral-line test never exercised because it has no DB at all.
- **Listing a directory is not traversing it.** Mode 0400 lets `iterdir` enumerate names while
  every child is unopenable, so enumeration alone still reported a locked-out transcript source as
  healthy. `session-index` has to traverse into the per-project directories to read transcripts, so
  that is what is now tested.
- The vanished-storage wording no longer says messages "were indexed from it", which claims
  per-message provenance this does not check. It says the session index holds them.
- **The existence probes sat OUTSIDE the error boundary, and what they do depends on the Python
  version.** Both the sessions DB and the transcript source were tested with `Path.exists()` /
  `is_dir()` before any `try`. Measured on the same sealed-directory fixture: **3.11.15 raises
  `PermissionError`**, so `doctor` dies with a traceback, and **3.15.0 answers False**, so the same
  install reads as ABSENT — a fresh machine with nothing indexed yet. One mode bit on a parent
  directory produced either a crash or a reassuring lie depending on the interpreter. A
  self-looping symlink answers False on **both**, so that case was silently wrong everywhere.
  Both probes are now `os.stat` inside the boundary, classified by errno: `ENOENT`/`ENOTDIR` really
  means absent, and anything else is reported as "cannot be inspected" / UNKNOWN rather than
  rounded to absence. First-run neutrality is pinned by its own control, so a fix that answered
  UNKNOWN unconditionally fails three tests.
- **The UNKNOWN remedy pointed at a report that is never printed.** It said "check the sessions DB
  (rekol doctor reports it below)" — but that finding *returns*, so the session-index checks which
  would have reported the DB never run. It read as if the answer were already on screen. The
  remedy now names the DB path and gives two real actions, split by cause: move a corrupt DB aside
  and rebuild, or fix permissions on an unreadable one.
- **A brand-new install reported `index is DEGRADED` because Claude Code had not created
  `~/.claude/projects` yet.** An absent projects dir has two causes with opposite meanings, and
  both were reported as a problem. At the **default** location it simply has not been created yet
  — Claude Code creates it on first use, and rekol can be installed before that happens — so
  nothing is wrong and there is merely nothing to index. That is now a neutral line naming
  `claude_projects_dir`, with no remedy.
  At an **explicitly configured** location the finding is unchanged and stays a PROBLEM: the user
  said where it is and it is not there, so `session-index` exits 2 on every SessionEnd — into
  `>/dev/null 2>&1`, where nobody ever sees it — and the index can never build (#165). The
  comparison is against the *resolved* default, so a relocated `CLAUDE_CONFIG_DIR` counts as the
  default too; there the path is correct and merely unpopulated.
  Verified end to end on a first-run shape: `index is healthy`, exit 0, with the neutral line
  present and no `✗ transcript source`. Measured for context while fixing it — on a live machine
  the directory predated the rekol install by nearly three months, so the ordinary case is that it
  already exists; the gap is narrow, and a first impression is a bad place to be wrong. `doctor`
  being red out of the box has shipped twice, and each time it taught people to ignore the report.
- **First-run neutrality hid a real failure: transcript storage that vanished.** The new INFO
  line was returned for any absent default directory, before looking at the index — so a machine
  that had indexed transcripts and then lost its `~/.claude/projects` was told "does not exist yet
  … nothing to index until a session has ended" over a populated database (Codex measured this
  against a real fixture). Those two states look identical on the filesystem; the index is what
  tells them apart. A populated sessions DB now means **lost storage**, with a PROBLEM, the message
  count as evidence, and a remedy. A machine that has never indexed anything keeps the neutral
  line, which is the point of the change.
- **`is_dir()` proves type, not access, and its False result covered three different states.**
  Both of these were pre-existing, and both were made invisible by the neutral line, so they are
  fixed with it rather than filed:
  a **regular file** at the projects-dir path entered the "does not exist yet" branch, and an
  **unreadable** directory was not detected at all — `doctor` reported embeddings and FTS as fine
  while enumeration raised `PermissionError` on every `session-index` run, into discarded output.
  Enumeration is what `session-index` actually does, so that is now what is tested, with the
  failure classified rather than read as "empty".
- **`doctor`'s own tests could fail from a stray `rekol` on the developer's PATH.** Since the
  console-script check walks PATH (#203), a leftover build shim made five tests in
  `test_cli_doctor.py` fail — Codex hit this under `uv run` and the suite only passed with a
  restricted PATH. The module now pins PATH to an empty directory, so the host cannot influence
  it; tests that care about PATH still set their own. Reproduced before fixing and confirmed after:
  a broken `rekol` early on PATH fails five tests before, none after.
- **The guard checker missed a bypass hiding among guarded calls (#223, review).** A **mixed**
  file — a valid `rekol_bats_run_destructive` wrapper plus one `@test` doing
  `d="$COMPONENT_DIR"; run "$d/uninstall.sh"` — reported `guard violations: 0`. The checker required
  `COMPONENT_DIR` on the same line as the script name, so the split assignment meant the `run` line
  looked like prose that merely names the script, while the wrapper above satisfied every other
  rule. The existing `reject-split-variable-invocation.bats` fixture has no guarded call at all, so
  the anti-vacuity rule caught it — proving the checker notices an **empty** inventory, not that it
  notices a bypass.
  It keys on the **runner** now (`run`/`bash`/`sh`/`exec`/`source` of a path ending in one of the
  scripts), however that path was spelled, because spelling is what three rounds of this check kept
  failing to enumerate. All nine existing fixtures still classify correctly, the accept case
  included, and `reject-split-variable-among-guarded.bats` covers the mixed shape — picked up
  automatically by the loop that asserts every `reject-*` fixture fails.
  Two residual gaps from the same review are filed as **#256**, with the measurement that matters:
  `setup_file`'s real install already sandboxes `HOME` alongside `REKOL_HOME` and the `XDG_*` dirs,
  so it is not the 2026-08-18 shape; and adding `HOME` to the guard's override list would make it
  refuse on every run, because the guard looks before `HOME` is overridden per invocation.
- **The sandbox guard never resolved the FINAL path component, and the checker could still be
  blinded (#223, Codex).**
  `cd -P` resolves every component of a *directory* path, so an escaping directory symlink was
  caught — but the leaf was only ever examined by name. `$TESTROOT/external-link.json`, a symlink to
  a file outside the sandbox, passed the check while the destructive call wrote or deleted the
  target. The chain is now walked by hand (there is no `readlink -f` on macOS bash 3.2) and a loop
  or an absurd chain **fails closed**, because the target is unknowable. A contained symlinked leaf
  is still accepted, so the guard does not start crying wolf.
  And **zero recognised invocations is now a violation.** Codex assembled the path across two
  assignments — `exe="$COMPONENT_DIR"; exe="$exe/uninstall.sh"` — so no single line carried both
  halves, the inventory came back empty, and the checker printed `guard violations: 0`: the same line
  it prints when every invocation is correctly guarded. That is the second time a refactor blinded
  it, the first being the indirect `${script}` form this PR itself introduced, so the empty
  inventory is reported rather than passed. The fixture is in the suite.
- **The bats sandbox guard: a `..` component is refused on sight, there is one invocation site, and
  an existing file target no longer false-refuses (#223, Codex).** Containment already caught an
  escape, but only by *reading the filesystem*, so its answer was true only for the instant it was
  asked — a `..` traversal through a symlink replaced between the check and the delete is a real
  TOCTOU. The spelling is now refused before anything is resolved, in both the target check and the
  override check, matched on the path **component** so `..foo` and `foo..bar` stay ordinary names.
  The six wrappers are **one**, `rekol_bats_run_destructive`, whose first statement must be
  verbatim `assert_destructive_targets_sandboxed || return 1` — and the checker verifies that exact
  form rather than accepting a guard call anywhere above the invocation. Six wrappers meant six
  copies of the guard and six places one line could be deleted while the suite stayed green.
  **That change nearly shipped the checker blind**, and it is worth recording why: the one wrapper
  runs `"${COMPONENT_DIR}/${script}"`, the checker was keyed on the literal script names, so it
  found *zero invocations* and printed "guard violations: 0" — the same line it prints when
  everything is guarded. Codex's rejected-shape fixtures caught it. The checker now recognises the
  indirect shape, and a wrapper defined with nothing recognisable inside it is itself a violation.
  Canonicalisation also **refused a legitimate file**: it stopped at the deepest existing path and
  `cd`-ed into it, so a regular target — a manifest, `REKOL.md`, a recorded binary — failed the
  `cd` and read as outside the sandbox. It resolves through the containing directory now, and a
  file behind a directory symlink pointing out is still refused.
  The rejection fixtures are **files** under `tests/fixtures/guard/`, one accept and seven reject,
  shared by a bats test and a pytest. They began as heredocs inside `test_uninstall.bats`; the
  checker scans that file line by line, so a heredoc holding a second wrapper read as a real second
  wrapper and the suite flagged itself.
- **The new bats sandbox guard was loaded but never called in `test_install.bats` (#233).** That is
  the same defect the guard was written to remove — the copy it replaced there was never called
  either — and it mattered most in `setup_file`, which performs a real install and
  `rekol index rebuild`: the exact path that rebuilt a developer's real index with test vectors on
  2026-08-18. The refusal now runs in both `setup_file` and `setup`, with a test asserting the call
  exists rather than merely that the file loads the guard.
- **The sandbox check compared spelling, not containment (#233).** `$TESTROOT/../../outside` carries
  the right prefix and resolves somewhere else entirely, so it passed. Both sides are now
  canonicalized — resolving `..`, `.` and symlinks, and handling paths that do not exist yet by
  resolving their deepest existing ancestor — and the comparison requires a path component
  boundary, so a sibling named `${TESTROOT}-evil` cannot pass as inside the sandbox either. A
  symlink whose name is inside the sandbox but whose target is outside is now refused, which the
  string comparison could never have caught.
- **The invocation checker accepted a comment as a guard (#233).** It matched the guard's name as a
  substring within the eight lines above an invocation, so a comment mentioning it satisfied the
  check — text proximity is not a rule. It is now structural: `uninstall.sh` may be invoked only
  inside a `run_uninstall*` wrapper, every such wrapper must call the guard as an executable
  statement, and no wrapper may invoke before that call. The five remaining hand-rolled invocations
  were converted into wrappers, so there is no longer any path to the uninstaller that a new test
  could take unguarded. Proven against three controls, each of which must fail and does:
  a comment-as-guard, a direct invocation in a `@test` body, and a guard placed after the
  invocation.
- **The bats suites' safety rested on one `unset` line continuing to exist.** QA measured that
  deleting it makes the suite delete the developer's real tools home — venv and hooks included —
  because `uninstall.sh` removes what it is pointed at and an inherited `REKOL_TOOLS_HOME` points
  it at the live install. The suite still reports pass, so the failure is silent: every rekol hook
  on the machine fails afterwards, the same reports-healthy-while-dead shape as #203 and #143.
  Two guards existed and neither stopped it. Traced: `assert_sandboxed` was called at **2 of 20**
  destructive sites and both calls passed the *index dir* — nothing ever asked it about the tools
  home. It was not defeated, it was never consulted about the path that got deleted. A second copy
  in `test_install.bats` was never called at all.
  Both copies are replaced by `tests/sandbox_guard.bash`, shared so they cannot drift, and the
  habit is replaced by a property: every destructive invocation now goes through a helper that
  refuses when an override resolves outside the sandbox, and a test asserts no invocation in the
  file can skip that check. The refusal sits at the destructive call rather than in `setup()` on
  purpose — refusing merely because `REKOL_INDEX_DIR` is *set* would refuse on every ordinary
  machine, since #164 exports it into every session, and a guard that cries wolf gets deleted.
  The allowlist also no longer accepts a blanket `/tmp/*`, which reached any other suite's sandbox;
  `mktemp -d` output was already covered by the `TESTROOT` branch, so it bought nothing.
  Both halves are proven able to fail, not just to pass: with one guard call deleted the
  invocation check reports the exact line, and with the clearing deleted the suite fails in clean
  CI rather than only on a machine that happens to have a leaked override.
- **`rekol.config.yaml` was truncated before being rewritten (#238, product).** It is read, mutated
  and written back whole, so a fault between the truncation and the write emptied it and lost keys
  with nothing to do with the change being made — the embedding model, archive settings, whatever
  else the user had set. It goes through the same staged-and-replaced helper now, and its exemption
  from the rule check is gone rather than annotated.
- **A new memory was forced to mode `0644`, exposing it to every local account (#238, Codex).**
  Under `umask 077` `Path.write_text` produces `0600`, so `capture` kept a new memory private; the
  atomic-write change staged with `mkstemp` and then `chmod 0644`, which made it world-readable. A
  regression introduced by that change, and a security-relevant one.
  The stage is created with `os.open(..., O_CREAT|O_EXCL, <mode>)` so the **kernel** applies the
  umask — no reading or restoring of the process umask, which would be racy in a library. An
  existing target still keeps its own mode, so an edit cannot silently re-permission a file the user
  chmod'ed. Controls at umask 077 and 022, across a new memory, a new proposal and a dangling link's
  target, plus the counterpart that an existing `0640` file stays `0640` under either umask.
  `propose` now reports a write failure with the same one sentence as the other commands rather than
  a traceback.
  **The private content went into a readable temporary first (#238, Codex + product).** The first
  version of the above still staged at `0666 & ~umask` and copied the target's mode on afterwards,
  so under `umask 022` editing a `0600` memory wrote its whole content into a **world-readable
  `0644`** temporary — and a kill before the `chmod` left that file in the memory home. Another
  regression from this change, in the same place, which is why the rule is now stated rather than
  reasoned about each time: **at no point can the staged file be more readable than the file it will
  replace.** The target's mode is read before the stage exists and passed to `os.open`, which the
  kernel can only narrow, so the stage is never wider than the final file from the instant it exists
  — and the exact mode is restored before publication, so a tight umask does not tighten the file
  either. If the target's mode cannot be read at all, the write is refused rather than guessed at.
  The test probes the **descriptor** with `os.fstat` at the moment before the first byte, because
  checking the published file's mode would pass whatever the stage had been.
- **A symlinked `rekol.config.yaml` is written through, and that is deliberate (#238, product
  answering Codex).** `os.replace` acts on the name, so publishing without following the link would
  leave a regular file in the memory home holding the new keys while a dotfile repo kept the old
  ones — the config silently detached from the one the user edits. Memory files reached through a
  link out of the home are refused, which is the opposite call: a link leaving the home is an
  anomaly for a memory and the normal case for a config. Documented at the call site with a test
  asserting the link survives as a link and its target holds the new content.
- **The atomic-write property test claimed more than it checked (#238, Codex).** It scanned a fixed
  five-module list while the change claimed a new command would be caught automatically. It now
  scans **every** module under `src/rekol` with an explicit reviewed exemption list — each entry
  carrying the reason it is safe — so a new module must be looked at before it can pass. It also
  sees `Path.open("w")` and `r+`, which the control for the scan caught it missing, and the test
  states the shapes it still cannot see (a mode in a variable, `os.open`, `shutil.copy`, a path
  computed at runtime). Two further controls: an exemption for a module that no longer truncates is
  reported as a dead decision, and the five commands this fixed cannot be made to pass by exempting
  them.
  One exemption is a **known gap** rather than a safe case, and is labelled as such: `config.py`
  truncates `rekol.config.yaml` inside the memory home.
- Documented limits of atomic publication, in the helper where they apply rather than in a commit
  message: hard links to the target are broken, extended attributes and ACLs are not carried over,
  ownership comes from the staged file, and durability of the directory entry is not claimed (the
  bytes are fsynced before the rename; the rename is not).
- **A symlinked memory would have been forked rather than edited (#238, product).** `os.replace`
  acts on the **name**, so publishing over a symlinked memory deleted the link, left a regular file
  in the home holding the new text, and left the **real file with the old text** — the user's edit
  in a different place from the file they believed they edited, with nothing reporting it. This was
  a regression introduced by the atomic-write change itself: `Path.write_text` follows a symlink, so
  `confirm` on a symlinked memory works on `main`. The target is resolved first and the temporary is
  staged beside it, which also keeps the `os.replace` on one filesystem when the link points off the
  home. A dangling link is followed too, matching `write_text`, and the mode preserved is the
  **target's**. Tested contained, pointing outside the home, dangling, and dangling into a missing
  directory.
- **`tempfile.mkstemp` sat outside the error boundary (#238, product).** A read-only directory or an
  exhausted inode table raised a raw `OSError` traceback instead of `AtomicWriteError`, which breaks
  the one promise this module makes — that a failure says in one sentence whether the file survived.
  A consequence accepted deliberately: a writable file in a read-only directory used to be editable
  and now fails whole, with that message.
- **Editing a memory destroyed it first, then wrote the replacement (#238).** `Path.write_text`
  and `open(path, "w")` **truncate before writing**, and that is how `confirm`, `flag-suspect`,
  `invalidate` and `review --confirm` all edited a memory in place. A fault between the truncation
  and the write — ENOSPC, a kill, a full disk on a synced home — left the file truncated or empty,
  and the truncated copy was the only one. `capture` had the same shape with a different
  consequence: an interrupted write left a 0-byte or half-written `.md` under a real name, which
  the indexer reads as a memory.
  There is now one way rekol writes such a file (`rekol.atomic_write`): stage a temporary in the
  same directory, `fsync` it, then `os.replace` it over the target. The name holds the old content
  or the new content, never a partial write, and a failure says **"the file is UNCHANGED"** in one
  sentence instead of raising a traceback that leaves the user unable to tell whether their memory
  survived. The retry after a failure works, and leaves one file rather than two.
  **Keyed on the rule, not on the call sites.** A property test parses each module that writes
  into the memory home and fails on any `write_text` / `open(…, "w")`, so a new command that
  writes a memory the old way fails CI even though nobody thought to add a test for it — which is
  the point, since each of the sites this fixed was added by someone who did not know the rule
  existed. The detector has its own control, because a property test that cannot fail is worse
  than no test. It found two sites beyond the ones in the original report, in `propose`.
  The staged name is `.partial-<random>.md.tmp`: a leading dot is **not** enough on its own, since
  `Path.glob("**/*.md")` matches dotfiles and that glob *is* the indexer's walk. The test proves
  that premise rather than asserting it. The replaced file's mode is preserved too — `mkstemp`
  creates `0600`, so a naive replace silently tightened permissions on every edit.
- **A command that merely MENTIONED rekol's reindex script was still softened (#235, Codex).**
  Recognising it with `endswith` accepted `false && <path>/hooks/auto-reindex.sh`,
  `my-gate --config <path>/hooks/auto-reindex.sh` and `printf <path>/hooks/auto-reindex.sh`. Codex
  **executed** the first and watched exit 1 become exit 0 — #235 through a different door.
  The next version required the whole command to be "one path token", as a character class — which
  then accepted `false${IFS}<path>/hooks/auto-reindex.sh`: no whitespace in the string, a word break
  the moment the shell expands it, and two commands where the installer counted one. Codex found
  that too.
  **Ownership is a positive allowlist now (product ruling, 2026-11-07),** because each round of
  character class closed one shape and left the next one open: a class describes what we thought of,
  not what we wrote. A command is rekol's only if, after stripping the one suffix rekol appends, it
  is a supported prefix (`$HOME`, `${HOME}`, `~`, or an absolute path) followed by **literal** path
  components — no `$`, `{`, `}`, backtick, quote, whitespace or operator — ending in
  `/hooks/auto-reindex.sh`. A string that passes cannot expand into anything but itself, which is
  the property the class never had. Anything else is **not ours and is left alone**: a wrong "not
  ours" leaves a stale hook in place, which is much better than altering a command of the user's.
  The list was checked against every form released versions actually wrote (git history plus the
  v0.5.13 fixtures): the pre-rebrand and pre-#172 literal `$HOME/...` paths and today's absolute
  tools-home path, each bare or with the suffix. Two forms the installer *can* write are refused and
  said so in the code — a relative `--tools-home`, and a tools-home containing a space — because
  both describe a hook that is already broken before rekol touches it. All four attack shapes are
  negative controls and the two that gate on failure are **executed**, so "it stopped blocking"
  cannot pass as a byte comparison.
- **De-duplicating the reindex hook deleted hooks rekol does not own (#235, Codex).** It dropped
  whole later `PostToolUse` **blocks** whose text contained `auto-reindex.sh`, so a hook of the
  user's sharing such a block was **deleted** — worse than the softening this issue is about, since
  a softened hook is recoverable from the backup and a deleted one is not. It now removes only the
  owned handler, keyed on the same ownership predicate as the softener, and leaves everything else in
  the block; a block left with no hooks at all is dropped as debris.
  Reachable on an ordinary path, not just a hand-edited file: two reindex blocks arise from #236's
  duplicate-append behaviour on an un-upgraded install, and a user hook added to the newer block
  would then be destroyed by the next install.
- When the set of rekol-owned commands cannot be collected, the soft-fail pass is skipped — the safe
  direction, since it cannot touch a hook of the user's — and **says so** rather than leaving a
  failed collection indistinguishable from a settings.json that holds none of rekol's hooks.
- **The installer stopped a user's blocking hook from blocking (#235).** The soft-fail step matched
  any command whose text contained `rekol` or `REKOL`, so a hook of the user's own got
  ` 2>/dev/null || true` appended — `my-tool --uses rekol --flag`, or anything living under a path
  such as `~/src/rekol-tools/`. That is not cosmetic: `|| true` forces exit 0 and `2>/dev/null`
  discards stderr, which is how Claude Code receives a block reason, so the hook silently stopped
  blocking after any install. **Present since #183 (2026-08-18), so in v0.5.13 and on `main`;** the
  original is backed up as `settings.json.bak-softfail-*`.
  Soft-fail is now applied by **whole-command equality**: a command is rekol's only when the entire
  string equals one the installer is about to write. That set is built from the rendered snippets
  themselves plus a handler allowlist — one definition, derived from the renderer, driving both the
  softener and the count — with and without the soft-fail tail, since Step 6.95 rewrites a pre-#159
  bare invocation to the current rendering *without* the tail and this step is what adds it.
  An anchored regex was tried first and was wrong in **both** directions (product measured it): it
  missed rekol's own `@TOOLS_HOME@/hooks/auto-reindex.sh …`, whose command starts with a path, and
  SessionEnd's `if [ -n "${REKOL_HOME:-…` notice — so a user with no hooks of their own was told the
  installer had left theirs alone — while *claiming* `rekol _hook x && my-gate` and
  `"$(my-wrapper)" --check "_hook foo"`, because the subcommand test was an unanchored substring.
  Both of those are now executed negative controls, and a pristine install and reinstall must print
  no preserved-hook line at all.
  Both directions are **executed** in the test rather than inferred, because inference is what let
  this ship: a user's blocking hook is byte-identical and still exits non-zero with its stderr
  intact, and rekol's own hook still exits 0 with its binary removed, which is the failure that
  could otherwise fail a `UserPromptSubmit` and lock someone out of Claude Code.
  **The "we left N hooks of yours alone" report was written, measured and removed.** Answering that
  needs the ability to tell a *legacy* rekol rendering from a user's command, and this change does
  not have it: on a v0.5.13 upgrade fixture with **no user hooks planted** it said `left 4`, and all
  four were rekol's own handlers still in the `$(command -v rekol || echo …)` form, because Step 6.95
  here only rewrites the pre-#159 bare invocation. QA measured the same thing as `left 3`, where the
  third was the old SessionStart loader before a later step adds its `: rekol:backend=` marker — the
  count was running in the middle of the install and describing a state that no longer existed by the
  end. It lands with #203, whose allowlist defines every rendering this installer has produced. What
  this change owns is narrower and complete without it: the softener no longer touches a command that
  is not rekol's.
  Also recognised as rekol's: `<tools-home>/hooks/auto-reindex.sh`, by the script name rather than
  the path, because that path has changed three times (`memory-tools` before the rebrand, then
  `$HOME/.local/share/rekol`, then `--tools-home`) and an older install's string is not this one's.
- **A model change crashed `session-index` and `search`, and the message then said to `rm` your
  transcript store (#229, #230).** Found independently by the 0.6.0 fan-out review and by QA.
  `SessionStoreModelMismatchError` was caught at **none** of `reconcile_embedder`'s four call
  sites. Three of them caught its *sibling* `SessionStoreDimMismatchError`, and an `except` written
  for one does not catch the other — so a user who merely changed `embedding_model` in their config
  got a full traceback and exit 1, with the useful message at the bottom. `session-index` is the
  SessionEnd hook path, whose contract — stated in that very file — is "never a crash".
  Four sites is **how** that was missed, so the fix is one site: `reconcile_embedder` is now called
  only from `guard_session_store_identity`, which reports both sibling errors and exits 2, matching
  what the curated path already did. A test walks the package's AST and fails if the call appears
  anywhere else, so a fifth caller cannot reintroduce partial handling. The now-dead
  `except SessionStoreDimMismatchError` in `cli_session_index` is **removed, not relocated** — the
  error has exactly one raiser, reached only through the helper, and a dead `except` that reads as
  a guard answers "is this handled?" with a yes nothing can reach.
  **The message now says what to CHECK before what to DELETE.** It led with
  `rm '<path>/sessions.db' && rekol session-index --full` — and this guard fires precisely when a
  test-configured run has resolved someone's *real* store through an inherited `REKOL_INDEX_DIR`,
  so that advice asked the operator to finish the destruction the guard had just prevented. It now
  names `REKOL_INDEX_DIR`, says it is the highest-precedence setting and used verbatim, and offers
  the destructive line only afterwards and only when the store's provenance is known. The width
  message had the identical defect and got the identical fix; fixing one would have left the advice
  pointed the wrong way on half the paths.
  On an **unstamped** store there is now no destructive remedy at all. The provenance sentinel was
  the literal string `"unknown (store predates identity stamping)"`, which rendered as *restore
  'unknown (store predates identity stamping)' in your config* — advice that cannot be followed,
  because it is not a model name. It is `None` now and the message branches on it. All four commands
  are exercised end to end, asserting the exit code **and** the absence of a traceback, since exit 1
  with the message under a stack trace would pass a "the message is present" check.
- **`rekol migrate` permanently deleted a real `MEMORY.md`, with no copy anywhere (#224).** Found
  by QA against `508b33f` and present in published **v0.5.13**. `write_retirement_pointer` called
  `legacy_pointer.unlink()`, and because `discover_files_in_dir` deliberately skips the name
  `MEMORY.md`, the file was neither imported nor archived first — after
  `rekol migrate auto --commit`, content that lived only there existed **nowhere on disk**. A
  directory holding only `MEMORY.md` was emptied and marked migrated, exit 0.
  The unlink could never do the job it was written for. Its stated purpose was removing a
  *tombstone* left by an older version of this tool, but both callers sit behind
  `has_migration_marker`, which already treats an old-format tombstone as retired — so a real
  tombstone never reaches it. **Everything it could delete was a user's own file.** And it was
  pinned by a test that wrote `"Real content."` and asserted the file was gone, so this was a
  decision carried from tombstones to real files, not a slip.
  `MEMORY.md` is now **moved into `old-memory-archive/`**, never unlinked. That still stops
  Claude Code's autoMemory injecting it — autoMemory cannot inject what is not at that path — and
  it is the same guarantee `install.sh` already printed ("originals stay in place or move to
  `old-memory-archive/`") and `archive.py`'s header already claimed ("the only byte-complete
  copy of each original"). Both were false; now they are true.
  An earlier archived copy is never overwritten (`MEMORY-1.md`, `MEMORY-2.md`, …), because the
  archive holds the only original and one silent overwrite is one unrecoverable file.
  **The move is reported in dry-run and in the real run, and deliberately not gated on
  `--quiet`** — `install.sh --migrate` passes `--quiet`, and QA measured the deletion producing
  no output at all on that path, so the one destructive step was also the most silent. Dry-run
  previously said "would migrate N" and never mentioned `MEMORY.md`, so the only destructive step
  was the only one a user could not preview.
  Reach: `migrate auto --commit`, `migrate repo <path> --commit`, and `install.sh --migrate` —
  which is the flag the README advertises for importing auto-memory, so the users who hit this
  were the ones trying to keep theirs. A plain `install.sh` was unaffected.
- **A retired directory could abandon files in subdirectories without saying so (#225).**
  Discovery is deliberately non-recursive, but retirement is permanent: a nested `.md` was left on
  disk, never imported, never archived, and no later run looks — while the marker claimed the
  originals were in `old-memory-archive/`. Migration now names every such file and says plainly
  that they are neither imported nor archived, and the marker says "files discovered here"
  rather than implying everything. The retirement invariant held only because discovery defined
  what counted.
- **A hook could resolve an orphaned `rekol` on its PATH instead of the install's own, starting a
  session with no memory (#203).** Hook commands rendered as
  `"$(command -v rekol || echo '<recorded path>')"` — PATH first, recorded path only as a
  fallback. The reasoning was that PATH stays accurate if the install is later moved, but it had
  the risk backwards: **PATH is the unreliable half in a hook environment**, and whatever it finds
  first wins. A stale console script left behind by another interpreter therefore beat a perfectly
  good recorded path, and because an interactive shell resolves the working wrapper first,
  `command -v rekol` was green and `doctor` passed while every hook ran the orphan.
  The order is now inverted: use the recorded path when it is executable, and fall through to
  `command -v` only when it is not. A moved or reinstalled tree still adapts — the recorded path
  stops being executable and the PATH lookup takes over exactly as before — but an orphan earlier
  on a hook's PATH can no longer hijack a working install.
  Proven end to end rather than argued: a regression installs for real, puts an orphan **first** on
  the hook's PATH, asserts that a bare `rekol` genuinely resolves to that orphan (so the test
  cannot pass vacuously), then executes every wired hook command and asserts the orphan was never
  run. Against the previous order **nine** hooks executed it.
  Two residual cases, both narrower and both visible: a recorded path that exists but is broken is
  a genuinely broken install, and `doctor`'s hook-executability check names it; and if the recorded
  path is *absent* — a tree moved without reinstalling — PATH resolution resumes and an orphan
  could win again, which is the cost of keeping a moved install working. Re-running `install.sh`
  re-records the path and closes that window.
- **Standing down for the Claude plugin skipped the `env.REKOL_HOME` block, leaving the memory
  system dark — and a missing `jq` triggered the same stand-down (release blocker).** Found by
  the 0.6.0 pre-release fan-out review, in #199's own change.
  Two defects, one cause: `install.sh` gated Step 7.5 — writing `env.REKOL_HOME`,
  `REKOL_TOOLS_HOME`, `REKOL_INDEX_DIR` and `REKOL_ARCHIVE_DIR` into `settings.json` — on
  `DO_HOOK`, the same flag the coexistence stand-down clears. But that block is **not hook
  wiring**, and `plugin/hooks/bootstrap` states the plugin cannot edit `settings.json` at all
  ("that is the whole point"), so nothing else supplies those keys. By Step 7.5's own comment,
  without them "the entire memory system is dark". #199's design says to stand down hook wiring
  *only*; a new `DO_SETTINGS_ENV` flag now holds it to that, tracking the user's explicit
  `--no-hook`/`--test-mode` intent and clearing on exactly one other condition — a
  `settings.json` that cannot be parsed, where rewriting it would destroy the user's file.
  Second, the detector's `rc=2` conflated "a plugin may be enabled, I cannot tell" with "jq is
  not installed". On any machine with an existing `settings.json` and no `jq` — Debian/Ubuntu
  minimal, Alpine, older macOS — the installer skipped every hook step, skipped the env block,
  printed a reassuring "hook wiring skipped", and exited 0. Without `jq` it cannot *mutate*
  `settings.json` either, so it could not create the duplicate backend the gate exists to
  prevent: there was nothing to guard against. It now continues, and the documented jq-less
  fallback (README: "without it the installer prints the snippet to merge by hand") is reachable
  again rather than dead code.
  The existing stand-down test could not see any of this: it asserted that hooks were empty and
  that an unrelated `env` key survived, which holds identically whether the env block was written
  or skipped. It now asserts `env.REKOL_HOME`, and a new test hides `jq` and asserts the install
  still succeeds and still prints the snippet. Both were confirmed to fail against the pre-fix
  installer. Hiding `jq` mirrors **every** PATH entry containing one — this machine has two —
  and asserts the hiding worked before relying on it; an earlier version used a hand-picked tool
  allow-list and the installer died with exit 127 while the test still passed.
- **CI's bats change-filter ignored `plugin/`, which `install.sh` now hard-depends on.** Since
  #199 the installer `die`s when `plugin/hooks/coexistence.sh` is unreadable, so a change under
  `plugin/` can break the primary install path with the install suite never running. Added
  `^plugin/` to the filter; a Python-only change still skips bats.
- **Two `doctor` tests were named for behaviour they never executed (#194).** Found in the
  v0.5.12 external review; the production fix (#188) was correct, the evidence for it was not.
  `test_shipped_index_files_are_excluded_from_coverage` never ran `doctor` and never built an
  install — it asserted only that two constants classify `REKOL.md` as non-memory, which cannot
  support its docstring's claim that a pristine install is healthy and exits 0.
  `test_doctor_excluded_line_is_generated_from_the_real_list` was worse: it searched
  `cli_doctor`'s **source** for the literal `deliberately excluded ({excluded_names})`, so it
  would have passed just as happily with `excluded_names` assigned a hardcoded stale string —
  a check whose result is identical whether or not the promised property holds, which is the
  exact bug class the line exists to prevent, sitting in the test written to prove it closed.
  Replaced with behaviour-level regressions: a `REKOL_HOME` seeded from the **real shipped
  template** (the way `install.sh` seeds one, so the shipped `REKOL.md` is genuinely on disk
  and genuinely considered), its index built, the real Click `doctor` command invoked, asserting
  exit 0 and healthy; plus an assertion that `doctor`'s **rendered** exclusion line contains
  every name in the exclusion constants. The constants-only check is kept under an honest name.
  Both were confirmed to fail against the defects they describe: hardcoding a stale
  `excluded_names` fails the rendered-line test, and removing `REKOL.md` from the exclusion list
  reproduces the original day-zero `index is DEGRADED` exit 1.
- **`install.sh` accepted any Python >=3.11, so it chose one the dependencies have no wheels
  for and the install died inside pip (#207).** The suitability probe was bounded only from
  below. When Homebrew's `python3` became **3.15** (2026-10-09) and torch still published
  nothing past `cp314`, every install that fell through to it — no 3.11–3.14 present, no `uv` —
  began failing with `ERROR: ResolutionImpossible` naming `sentence-transformers` and `torch`,
  which pointed at the dependency set rather than at the interpreter choice that caused it.
  This is what had the required `macos-latest` gate red across four consecutive merges, and it
  broke **real first-time installs** on any up-to-date Mac in that shape, not just CI.
  No commit caused it: the trigger was a new Python release, which is why the four red runs
  bisect "cleanly" to an unrelated change — the last green macOS run was seven weeks earlier,
  so a moving environment and a moving tree are indistinguishable across that gap.
  The accepted range is now bounded at **both** ends (`>=3.11`, `<3.15`), 3.13 and 3.14 were
  added to the probed candidates since torch supports them, and a too-new interpreter is
  refused up front with the same kind of guidance a too-old one already got. The hard-fail now
  lists every interpreter considered and why each was passed over — previously the installer
  announced which python it picked and said nothing about what it had rejected, so there was no
  way to tell a missing interpreter from a disqualified one.
  Raise the ceiling when torch ships the next `cpXXX` wheels; a test asserts the ceiling and
  the candidate list cannot drift apart in either direction.
- **A failing `setup_file` in the install tests reported no reason, which is why a red
  `macos-latest` gate went four merges undiagnosed (#207).** The one command that does the real
  work there — a full `install.sh` run — sent its output to `/dev/null`, so on failure bats could
  print only `not ok 1 setup_file failed` and the offending line. **The cause was discarded before
  the job log was ever written**, so re-reading that log could not have helped; `macos-latest` is a
  required gate, so `main` was unmergeable with nothing on record saying why.
  The output is now captured and printed in full on either failure path — a non-zero exit, or an
  exit-0 install that somehow left no `rekol` in the venv — and the real exit status is recorded at
  the call rather than read from `$?` inside an `if ! …` branch, where `!` has already turned it
  into 0. (That detail matters here: a wrong exit code in the very message meant to explain one.)
  `install.sh` had the identical shape at the index-dir resolution step — the interpreter's stderr
  to `/dev/null`, then `could not resolve the local index dir from the venv`: the symptom, with the
  traceback that explains it thrown away. Diagnosing one instance by hand cost four full installs,
  because a broken editable install, an import error, and a source tree being rewritten mid-install
  all produce that one sentence. The traceback is now part of the error.
  Diagnostics only — this does **not** fix the macOS failure, it makes the next run state it. This
  is the project's recurring defect shape (a report that reads the same whether or not the thing
  under test held) sitting in the diagnostics themselves.
- **The sessions index validated vector WIDTH only, so a test embedder could silently poison a
  real transcript corpus (#193).** `test-hashing` produces 384-dim vectors exactly like the
  default `BAAI/bge-small-en-v1.5`, and the store compared only dimension — so a test-configured
  run that inherited a real `REKOL_INDEX_DIR` wrote hash vectors into a real corpus with **no
  mismatch raised at all**. An incremental run mixed two semantic spaces; a full run replaced the
  useful population. Both stay structurally healthy and are searched as though they were real
  vectors. Two genuinely different *real* models at the same width collide identically, with no
  test embedder involved: equal dimension is not equal semantics.
  The sessions store now records the model that built it, in a `metadata` table mirroring the
  curated store's, and `reconcile_embedder()` owns both halves of the question for every reader
  and writer — the same chokepoint `reconcile_embedding_dim()` already used for width. Identity
  is checked first, because a model mismatch is wrong whether or not the widths agree, and
  "wrong model" is more actionable than "wrong width".
  **Existing stores keep working.** A populated-but-unstamped store — which is every `sessions.db`
  created before this change — adopts the configured model once and stamps it, *unless* that
  model is a test embedder **and the store already holds embeddings**, which is refused. The
  refusal is keyed on embeddings rather than messages because the harm is a mixed vector space:
  messages and the FTS index are model-independent, so a store with rows but no vectors has
  nothing a test embedder could semantically corrupt.
  Verified against a **copy of a real 137 MB, 34,189-message store**: the migration is additive
  (`CREATE TABLE IF NOT EXISTS` plus a stamp, no row rewrite), every message and FTS posting count
  was preserved, identity adopted correctly, and a subsequent test-embedder run was refused. The
  seven new tests were confirmed to fail against the dim-only behaviour.
- **`<env-time>` reported every gap in minutes, so long pauses were unreadable.** The formatter
  only ever did `divmod(seconds, 60)`: a 17-hour pause rendered as `1071m 0s` and an 8-day one as
  `12217m 54s`. Both correct, neither legible — and it is part of why `mac_setup` kept a duplicate
  of this whole hook rather than defer to rekol's (shipwright, 2026-10-08).
  Durations now use at most two units, largest first — `45s`, `5m 3s`, `17h 51m`, `8d 11h`. Two is
  the cap because a third never changes a decision, and each tier drops the smallest unit rather
  than accumulating, so the string stays short as the gap grows. A missing timestamp still reads
  `unknown` rather than `0s`: absence of data is not a zero-length gap, and collapsing them would
  let "never recorded" read as "just happened".
  The formatter moved to module scope so it is testable directly. The only existing assertion was
  `"<env-time>" in output`, which holds for every possible duration — a test that cannot tell
  `17h 51m` from `1071m 0s` is why this shipped. Thirteen cases now pin the boundaries (59/60s,
  3599/3600s, 86399/86400s, negative clamp, `None`), and were confirmed to fail against the old
  formatter.
- **A stood-down plugin went silent after one session, so an inert plugin looked like a working
  one (#201).** The coexistence stand-down printed its explanation once, gated on a state file,
  and exited silently every session after. A user who installs the plugin on a machine that
  already ran `install.sh` sees a single message they may scroll past — and `doctor` has no
  backend awareness to contradict them afterwards, so nothing they can run reveals that the
  plugin is doing nothing. Same reports-healthy-while-dead shape as #143 B2 (auto-resume
  reporting ENABLED while inert) and #159 (hooks failing silently behind `|| true`).
  The verbose explanation still prints once — repeating four lines every session would blow the
  session-start context budget the design sets out (acceptance criterion 13) — but every
  subsequent session now carries one line: `plugin inactive — the install.sh backend owns the
  hooks (rekol doctor)`. The noise is self-limiting: it appears only while two backends are
  installed, and stops when the user picks one.
  The original test ran bootstrap **once**, so it could not distinguish "announces every session"
  from "announces once then goes silent" — which is why this shipped. The test now runs it three
  times and asserts the later runs are not silent; confirmed to fail against the once-only gate
  with `second run was SILENT`.
- **Plugin and script installs no longer activate duplicate hook backends (#167).** The Claude
  plugin now stands down when it detects an existing `install.sh` deployment, including legacy
  installs that predate the backend marker. Conversely, `install.sh` detects an enabled Rekol
  plugin and skips its own hook wiring. Detection failures are handled conservatively so an
  unreadable settings file or unavailable parser cannot silently enable both backends.

### Documentation
- **README now covers a Python that is too NEW (#207).** The install troubleshooting list
  explained a too-old `python3` but not a too-new one, which became the live failure the moment
  Homebrew's `python3` reached 3.15 — so the newly-added refusal would have sent people to a
  list that did not mention their case. Documents the supported `>=3.11, <3.15` range and why
  the ceiling exists (torch publishes no wheels past `cp314`), and corrects the set of
  `python@3.x` opt-prefixes the installer probes, which now includes 3.13 and 3.14.

## [0.5.13] - 2026-09-02
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

### Changed
- **The #170 hook-execution test now asserts its own precondition.** It runs every installed hook
  with `rekol` off the PATH, but never checked that `rekol` was actually unreachable — so on a
  host where it resolved anyway, the test would pass while proving nothing. Credit to QA, who hit
  the mirror image: their probe used `bash -lc`, a **login** shell, which sources the rc files the
  installer edits and re-adds `BIN_DIR` to PATH — testing the exact opposite of the #159 condition
  and passing for the wrong reason. They caught it only because they asserted the precondition
  explicitly; that habit is the general answer to this bug class, not a patch for one instance.

### Fixed
- **The real-index guard was bypassed by `rekol search` (release-review blocker).** #185 claimed
  the guard lived "where the destructive act happens". It did not — it was called only from the
  `rekol index rebuild` CLI wrapper, while the destructive act is `Indexer.rebuild()`'s
  `os.replace(tmp_db, live_db)`. `cli_search` reaches that same replace through
  `ensure_curated_schema_current(auto_rebuild=True)` with **no guard at all**, and a legacy
  (pre-identity) index has no metadata table — so `check_model_identity` passes it through as
  "unknown" rather than raising.
  A throwaway process configured with `test-hashing` that inherited the real `REKOL_INDEX_DIR`
  could therefore replace a user's legacy curated index with test vectors by running an ordinary
  **interactive search**. Same outcome #185 claims to refuse, through a different door — and
  placing the guard at the wrapper was the same mistake as the bug it fixes: asserting the
  invariant where it was convenient rather than where the destruction happens.
  The guard now sits immediately above the `os.replace`, so every caller of `rebuild()` is
  covered, and the refusal is reported by `search` rather than escaping as an uncaught traceback.
- **The guard failed OPEN on any existing index whose provenance it could not read.** A
  pre-identity schema, a missing metadata row, and an unreadable/corrupt DB were all treated like
  "no file at all", and all permitted. Only the absent-file case actually proves there is nothing
  to destroy.
  The stated justification — that a guard blocking the repair of a damaged index is worse than the
  bug — was on the wrong axis. This guard only ever refuses when the **incoming** model is a test
  embedder; a legitimate repair uses a real model and never reaches the decision. So failing
  closed blocks no repair, while failing open left the original destructive scenario open for
  exactly the oldest and least replaceable indexes. Two tests asserted the unsafe property by
  name; both are inverted, and a new test pins that a real-model repair of a corrupt index is
  still unimpeded.

- **The migration marker told users to delete the only byte-complete copy of their originals.**
  It said *"archived originals in old-memory-archive/ — safe to delete after 1 week"*, while the
  same release documents that migration strips unrecognised legacy frontmatter which then exists
  **only** in that archive. Those two statements cannot ship together: it was a live, destructive
  instruction the release itself proves false. The marker now says to KEEP the archive unless the
  migrated copies have been checked, and states plainly that unrecognised keys survive only there.
  No time-based deletion promise remains, and the module docstring records why one must not be
  reintroduced without a metadata-preservation contract.
- **The `--no-llm` exit-code test never invoked the CLI.** It was named `..._exits_zero`, called
  `migrate_dir` directly, and could not observe an exit code at all — claiming a
  production-boundary behaviour it did not exercise. That is the same structural failure this
  release describes elsewhere, committed in the act of fixing it. Renamed to what it actually
  asserts, and the boundary is now covered by real Click-invoking tests: defaulted `--no-llm`
  → exit 0 with the marker written; a hard failure → exit 1 with no marker; a mixed run → exit 1,
  no marker, failed original still retryable. Confirmed to fail against the earlier revision.
- **Dry-run claimed committed-state facts.** It printed that files "were placed in knowledge/",
  originals "are in old-memory-archive/", and the directory "is retired" — while writing,
  archiving and retiring nothing. The wording now branches on dry-run.

- **The first #166 fix narrowed the bug instead of fixing it — caught in external review.**
  Removing `len(errors) > 0` from the retirement condition left `migrated > 0`, so **one**
  successful file tombstoned the whole directory and every failed sibling was abandoned
  permanently. Worse, the `--no-llm` path — which is what `install.sh` and `cli_init` both use,
  i.e. the actual production path — recorded no error at all, exited **0**, journaled `MIGRATED`,
  and retired the directory. The tested path was fixed while the shipped one was not.
  The invariant is now explicit: **a directory may be retired only when every discovered file
  reached a terminal safe state** — imported (by any method) or skipped as a duplicate. One
  unimported file blocks retirement for the whole directory, and since successful originals have
  already moved to `old-memory-archive/`, a re-run naturally sees exactly the files that still
  need work.
  `report.errors` now means *unimported*; diagnostics about imported-but-poorly-described files
  live in `report.warnings` and deliberately do not block retirement. Keeping both in one list is
  what made the two defaulting paths disagree about the durable outcome for identical results on
  disk.
  The CLI no longer advertises a reclassification that has no command behind it (the module
  docstring documents `redo <file>`; only `auto` and `repo` are implemented — tracked separately).
  The regression test that was supposed to cover this was **vacuous**: it created one file, let it
  succeed, and asserted the marker, while its name claimed it proved partial success was safe. It
  is replaced by a real mixed-outcome test, confirmed to fail against the previous fix.

- **`rekol migrate` could abandon legacy memory permanently and report success (#166).** Four
  things conspired, and the install path hits all of them:
  - **The LLM failure was swallowed.** `except LLMUnavailable: pass` — nothing recorded, nothing
    printed. A run where the LLM was unavailable for *every* file produced a full set of stub
    classifications in silence.
  - **The fallback was tallied as a success.** The knowledge default carried `method="heuristic"`,
    so a total classification failure printed as `migrated 87 (heuristic=87, llm=0)` —
    indistinguishable from a real frontmatter-driven migration. It is now `method="defaulted"` and
    counted separately: a stub is not a classification.
  - **The tombstone was written even when everything failed.** `len(report.errors) > 0` counted as
    progress, so a run in which every file failed still marked the source directory retired — and
    a re-run then prints `skipped — already retired` and refuses to try. The files sit
    un-migrated in their original directory and rekol never looks again. Retirement now requires
    *genuine* progress. A repeated retry is visible and recoverable; an unnoticed tombstone is
    neither.
  - **The documented remedy could not work.** Both automated callers pass `--no-llm`, so the
    advice to *"re-run `rekol migrate auto --commit`"* was a guaranteed no-op once the first pass
    had written the tombstone.
  Plus: the command now **exits non-zero when any file failed**. `install.sh` runs it inside
  `if … ; then log_journal "MIGRATED legacy memory (auto)"`, so a silent zero exit made the
  durable install record claim success over a migration in which nothing worked.
  `DEFAULT_MODEL` is now overridable via `REKOL_MIGRATE_MODEL`. Hardcoding a model id is a time
  bomb: the day it is retired, every file silently falls back to a stub classification, for
  everyone, at once.
  **The message body is never destroyed by migration** — `archive_file()` moves originals into
  `old-memory-archive/` rather than removing them, and every failure path leaves the file on
  disk. This was silent *abandonment* reported as success, not destruction of content.
  **That claim was originally written as the stronger "no data was ever deleted", and external
  review was right to reject it.** Migration synthesises a new frontmatter whitelist and
  `_strip_frontmatter` discards the original YAML, so any unrecognised legacy key survives
  *only* in the archived original — which the migration marker tells the user is "safe to delete
  after 1 week". Follow that instruction and those fields are gone. The transformation policy may
  be fine, but the archive is not safely deletable without an explicit metadata-loss contract,
  and the unconditional claim should not have been used to categorically distinguish this from
  data loss. Tracked separately.
  An existing test asserted the buggy behaviour (`test_migrate_dir_marks_retired_even_when_all_files_fail`),
  deliberately, to stop a broken corpus being retried forever. That trade was wrong and the test
  is now inverted, with the reasoning recorded in it.
- **CI went red on `main` with no code change, blocking every PR.** `pyproject.toml` pinned
  `mypy>=1.11` with **no upper bound**, so CI installed a newly-released mypy that added checks
  and flagged two pre-existing imprecisions — in files nobody had touched. `main` itself failed,
  and so did every open PR, for reasons unrelated to their contents.
  Both flagged lines were real, if minor: `click.get_text_stream("stdin")` resolves to `object`
  under the newer checker (not callable), and `click.Path` is typed `str | bytes | PathLike`
  while `_invoke` builds an argv list — where a `bytes` element would fail at *runtime*, not just
  type-check. Fixed properly rather than silenced with `# type: ignore`.
  `mypy` is now upper-bounded (`>=1.11,<2.2`). Type checkers add checks in minor releases; that
  is a toolchain upgrade to take deliberately, not one that arrives overnight and turns the tree
  red. Verified the affected hook behaves identically before and after — same output with a
  payload, with no stdin, and exit 0 in both.

- **Every new user's first `doctor` reported `DEGRADED` and exited 1.** Found by QA on a
  **pristine** v0.5.3 install: `curated coverage: 5/6 — 1 invisible to search: REKOL.md`, with
  the remedy *"fix the frontmatter"* — for a file the user did not write, which would not help,
  because `REKOL.md` is outside the indexer's scope **by design**.
  rekol ships `REKOL.md`, seeds it from its own installer, and injects it verbatim into every
  session via the SessionStart hook — so it is already maximally available and indexing it would
  be redundant. It simply was not on the exclusion list: `NON_MEMORY_FILES = ("MEMORY.md",)`.
  The cause was a **half-finished rename** — `MEMORY.md` → `REKOL.md` moved the template and the
  hook and left the exclusion tuple behind. Same shape as #177, where the repo files were renamed
  while the installer kept writing the old name.
  This mattered more than a cosmetic red tick: #157/#158 had *just* made `doctor` trustworthy
  after it audited the index against itself. A checker that is red out of the box for a
  non-problem trains users to ignore it and buries a genuinely skipped file in permanent baseline
  noise — undoing the fix rather than polishing it. It also broke anything gating on `doctor`'s
  exit code (CI, QA's harness, our own post-install check).
  `doctor`'s "deliberately excluded (…)" line is now **generated from the exclusion list** rather
  than restating it in prose — the old hardcoded `(tasks/, MEMORY.md)` would have gone stale
  silently, which is precisely what that line exists to prevent.

- **The bats install suite could still destroy the real curated index — and this is how it
  actually happened on 2026-08-18.** `setup_file()` writes `embedding_model: test-hashing` and
  runs a REAL `install.sh`, which runs `rekol index rebuild`. `REKOL_INDEX_DIR` is resolution
  order #1 and is used verbatim, so inherited from a developer shell it pointed that rebuild at
  the **live** cache. **Running the install test suite was the thing that replaced a user's
  curated index with test vectors.**
  The v0.5.6 "hermetic bats" fix added the unsets to `setup()` only — but `setup_file()` runs
  FIRST and is the function that performs the destructive work, so the fix protected everything
  except the dangerous part. `setup_file()` is now hermetic too.
  Found because the v0.5.8 library guard **refused the write in production** when the suite was
  re-run — a harness-level guard could not have caught this, since the harness was the thing
  doing it.

- **rekol shipped `skill.md`; Claude Code documents `SKILL.md` (#177).** The docs specify
  `~/.claude/skills/<name>/SKILL.md` — 38 mentions, zero lowercase — and say nothing about
  case sensitivity. rekol wrote `skill.md`, which worked **only because macOS is
  case-INSENSITIVE**, so the two names collide into one file. On Linux they are different
  files, and whether a skill loads at all was resting on undocumented case-folding.
  The four shipped skills are renamed, and `install.sh` now reads and writes `SKILL.md`.
  Renaming the repo files while the installer still wrote the old name would have fixed
  nothing on a case-sensitive host, so a test asserts the installer too. A stale lowercase
  copy from an older install is removed, guarded with `-ef` so on a case-insensitive host we
  do not delete our own destination.
  **The guard asserts against git's recorded filename, not the filesystem's.** On a
  case-insensitive checkout `Path.glob("SKILL.md")` happily matches a file git records as
  `skill.md`, so a filesystem check literally cannot fail on the machine most likely to run
  it — it would pass for the wrong reason. Verified to fail against `main`.

## [0.5.7] - 2026-08-25
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

## [0.5.6] - 2026-08-25
### Fixed
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

## [0.5.5] - 2026-08-25
### Fixed
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

## [0.5.4] - 2026-08-25
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
