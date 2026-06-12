# Launch Runbook — making `rekol` public

This is the ordered procedure for flipping the repo from private to public. It exists
because the pre-launch CI setup uses a **self-hosted runner on the maintainer's machine**,
and that runner becomes a remote-code-execution risk the instant the repo is public. The
steps below must run in order, and the breaking steps must **not** fire before the
coordinated go.

## Launch date & gate conditions

**Target: Tuesday, June 16 2026** (fallback Wed June 17), per product
(`from-business/20260609-0332`). Run this runbook that morning **on product's explicit
coordinated go** — not on the date alone. The flip is gated on BOTH:

1. **macOS clean-install acceptance is green** — the primary platform, verified on a clean
   environment (see "macOS acceptance" below). This is the real remaining gate.
2. **The date arrives and product gives the explicit go.**

Until both hold, every breaking step here stays **held**.

## macOS acceptance (gate #1)

macOS is the primary platform and must be verified on a **clean** environment before the flip
(neither maintainer Mac is clean — both carry prior rekol state). Use a **borrowed/clean Mac**
(zero GitHub minutes; macOS CI runners bill 10×), and assert with the one-command check:

```sh
./install.sh           # non-interactive: preset REKOL_HOME to skip the prompt
rekol doctor --deep    # MUST exit 0
```

`rekol doctor --deep` is the acceptance backbone — a clean install can exit 0 yet be silently
broken, so it isn't enough that `install.sh` returns 0. `--deep` proves the index is non-empty
and schema-current, the embedding model **loads offline and embeds meaningfully** (catches the
silent mean-pooling degradation class), and **end-to-end recall** returns a known chunk.
Then spot-check: the wave-2 subcommands are present, skills installed, the SessionStart hook is
present and `REKOL_HOME` resolves. Record the result in `tests/acceptance.md`. (QA's hosted
`macos-latest` CI job calls the same `doctor --deep` — deferred post-launch to save the 10× minutes.)

## Why the ordering is load-bearing

During the pre-launch window, GitHub-hosted Actions minutes are exhausted, so CI runs on a
self-hosted Docker/Linux runner (`rekol-ci-runner`) on the maintainer's Mac, authed by a PAT
stored **outside the repo** at `~/.rekol-runner-pat`.

- A self-hosted runner attached to a **public** repo will execute arbitrary code from any
  fork's pull request → RCE on the maintainer's machine. So the runner must be gone *before*
  the repo is public.
- GitHub Actions **billing has since been added** (~2026-06-11), so hosted minutes now work on
  the private repo too. The runner nonetheless **stays serving CI until launch day** by
  product's decision (`from-business/20260609-0332`) — we do NOT decouple/deregister it early.

The resolution is an atomic-ish flip: deregister the runner, swap CI to hosted, then make the
repo public — close together, in this order. The hosted-revert change is **prepared and pushed
as an unmerged branch** (`revert/ci-hosted`) so it can be merged the moment the runner is down.

## Preconditions (do not start until all true)

- [ ] **Product gives the explicit go** for the public push (coordination channel).
- [ ] **macOS clean-install acceptance is green** (gate #1; `install.sh && rekol doctor --deep`
      on a clean Mac, recorded in `tests/acceptance.md`).
- [ ] Linux cold-clone acceptance is green (`tests/acceptance.md`).
- [ ] **Acceptance was run against the ACTUAL launch commit/tag**, not an earlier fix commit —
      `origin/main` keeps moving (version bumps, follow-up PRs), so run QA's `run-acceptance.sh`
      (+ `rekol doctor --deep`) once more on the exact commit being shipped, so the launch stamp
      is *verified*, not inferred. (Per QA `qa-to-dev/20260611-1656`.)
- [ ] **`.github/FUNDING.yml` is present on `main`** (Sponsor button live day one).
- [ ] `revert/ci-hosted` branch exists, is pushed, and has been eyeballed (see step 3).
- [ ] You are at a keyboard that can reach the maintainer's Mac (the runner host) and GitHub.

### Privacy / hygiene (public repos carry no machine-specific or personal identifiers)
- [ ] **Commit history authored under the personal identity only** — `git log --format='%ae' | sort -u`
      shows just the personal email, no employer/work address. (The early commits were re-authored
      from a work email; if any landed since, re-run the mailmap rewrite + re-tag + force-push.)
- [ ] **No internal planning docs in the public tree** (`docs/plans/` removed).
- [ ] **Test/acceptance docs describe environments generically** ("Intel macOS, real hardware" /
      "Linux arm64, Docker") — no named machine or person.
- [ ] **Public files use a project contact**, not a personal email where avoidable
      (CoC → `conduct@rekol.io`; README → `leon@rekol.io`/`hello@`/`security@`).
- [ ] **Final identifier scrub clean** — no stray usernames, hostnames, internal absolute paths
      (`/Users/<name>/…`), or employer names in tracked files:
      `git grep -niE "ticketmaster|/Users/[a-z]+|/home/[a-z]+" -- . ':(exclude)docs/transcript-archiving-*'`

## The sequence

### 1. Freeze merges to `main`
Announce a short merge freeze in the coordination channel. No PRs should merge between
deregistering the runner and the hosted gate going live — there'd be no working CI in that gap.

### 2. Deregister the self-hosted runner
On the runner host:

```sh
# Stop and remove the runner container (myoung34/github-runner). The container
# deregisters itself from GitHub on graceful stop.
docker stop rekol-ci-runner && docker rm rekol-ci-runner
```

Then confirm in **GitHub → Settings → Actions → Runners** that no self-hosted runner is
listed. If a stale registration lingers, remove it there manually.

### 3. Merge the hosted-revert CI (`revert/ci-hosted`)
This branch restores the hosted configuration:
- `on:` regains `push: { branches: [main] }` (post-merge runs are fine again on hosted).
- `runs-on:` goes back to a hosted image (`ubuntu-latest`, plus the `macos-latest` matrix leg).
- `actions/setup-python` is restored (hosted images need it; the self-hosted box didn't).
- the dropped `ubuntu-latest` portability job is restored.
- the bats-gating step (`Detect shell-script changes` → conditional bats) is **kept** — it's
  orthogonal to the runner swap and still useful on hosted.

Merge it. Verify the next PR (or the post-merge push run) executes on a GitHub-hosted runner,
not self-hosted, and goes green.

### 4. Revoke the runner PAT
The runner's PAT is no longer needed. Revoke it at **GitHub → Settings → Developer settings →
Personal access tokens**, then delete the local copy:

```sh
rm -f ~/.rekol-runner-pat
```

### 5. Flip the repo public
**GitHub → Settings → General → Danger Zone → Change visibility → Make public.** Confirm.

### 6. Post-flip verification
- [ ] No self-hosted runner attached to the repo.
- [ ] CI runs on hosted runners and is green.
- [ ] PAT revoked and local file removed.
- [ ] `install.sh` cold-clone path still works from the now-public URL (see `tests/acceptance.md`).
- [ ] Announce the freeze is lifted.

## Rollback

If anything fails after step 5, the repo can be flipped back to private from the same
visibility setting. If a hosted CI run fails for environment reasons (not a real defect), the
self-hosted runner can be re-registered with a fresh PAT as a temporary measure — but treat
re-attaching a self-hosted runner to a public repo as a security regression and remove it as
soon as hosted CI is sorted.

## Source of truth

The revert details mirror the header comment in `.github/workflows/ci.yml` (the `🚨 REVERT
BEFORE THE REPO GOES PUBLIC` block). If the two ever diverge, the workflow file's prepared
`revert/ci-hosted` diff wins — update this runbook to match.
