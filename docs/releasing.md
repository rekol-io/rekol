# Releasing rekol

The mechanics are three commands. The part worth reading is **severity** — it is
the only thing that decides whether existing installs ever hear about a release,
and it is easy to forget at exactly the moment it matters.

## Why severity is not derived from the version number

#159 was a patch-level fix that every existing install needed: hooks invoking a
bare `rekol`, exiting 127 in any session whose shell lacked an interactive PATH,
five of them failing silently. A rule like "minor bumps notify, patches don't"
would have stayed quiet about precisely the release that mattered most.

So severity is declared per release, by a tag `rekol update` can see.

## The three tiers

| Tier | How to tag it | What a user sees |
|---|---|---|
| *(default)* | `v0.5.4` | **nothing.** `rekol update` and `doctor` report it if asked |
| **High** | `v0.5.4.High` or `severity/high/v0.5.4` | one quiet line at session start |
| **Critical** | `v0.5.4.Critical` or `severity/critical/v0.5.4` | a line stating action is required |

**Silence is the default on purpose.** The loud tier only stays meaningful while
the quiet one exists; a channel that speaks every session is one people learn to
skip, and then the release that matters goes unheard too.

### Two encodings, pick per release

Both are visible in `git ls-remote --tags`, which is the *only* thing the cheap
check can see — an annotated tag's **message** lives in the tag object, not the
ref list, so severity written in prose would cost a full fetch or the GitHub API
(60 req/hr per IP; trips for a company behind one NAT).

- **Marker ref** — `severity/critical/v0.5.4` pushed *alongside* a plain
  `v0.5.4`. Keeps the release tag pure semver, so `scripts/bump_version.py` and
  every other tool keep working, and it is **correctable**: push or delete the
  marker without touching the release tag.
- **Suffix** — `v0.5.4.Critical` as the release tag itself. One tag instead of
  two, at the cost of a tag that is not valid semver. A tag is permanent, so a
  mis-marked severity cannot be fixed without moving a public tag.

Prefer the marker ref. Use the suffix when you want one command and are sure.

## Steps

```bash
# 1. main must be green, clean, and carry the version you are releasing
git checkout main && git pull --ff-only origin main
grep '^version' pyproject.toml

# 2. move CHANGELOG [Unreleased] into a dated section for that version,
#    with any ⚠️ action-required block FIRST — it is what a reader must not miss

# 3. tag
git tag -a v0.5.4 -m "v0.5.4 — <one line>"
git push origin v0.5.4

# 4. severity, ONLY if this release needs it
git tag severity/critical/v0.5.4        # or: severity/high/v0.5.4
git push origin severity/critical/v0.5.4

# 5. publish (this is the outward-facing step)
gh release create v0.5.4 --title "..." --notes-file <(sed -n '/^## \[0.5.4\]/,/^## \[/p' CHANGELOG.md)
```

## Before you tag, ask

- **Does an existing install need to act?** If re-running `install.sh` is
  required — new hooks, a `settings.json` migration, a repaired invocation —
  it is at least **High**, and the CHANGELOG needs an ⚠️ action-required block.
- **Is the fix invisible until acted on?** #159's failures were silent. A silent
  failure that a user cannot detect argues for **Critical**, because the only
  signal they will ever get is the one we send.
- **Would I want to be interrupted for this?** If not, leave it unmarked.

## Verifying it worked

```bash
rekol update --force        # should report the new version and its severity
rekol update --json         # {current, latest, severity, action_required, ...}
```

`rekol update` never installs anything — it checks and dismisses. An agent that
can update itself unattended is scheduled remote code execution from a GitHub
repo with no human in the loop, so the agent proposes and the human applies.

Forgetting a severity marker fails **quietly** — the release simply does not
announce itself. `doctor` reports the last *successful* check, so a broken
checker is visible, but nothing can tell you that you meant to mark a release
and didn't. That is why this file exists.
