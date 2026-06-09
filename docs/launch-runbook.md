# Launch Runbook — making `rekol` public

This is the ordered procedure for flipping the repo from private to public. It exists
because the pre-launch CI setup uses a **self-hosted runner on the maintainer's machine**,
and that runner becomes a remote-code-execution risk the instant the repo is public. The
steps below must run in order, and the breaking steps must **not** fire before the
coordinated go.

## Why the ordering is load-bearing

During the pre-launch window, GitHub-hosted Actions minutes are exhausted, so CI runs on a
self-hosted Docker/Linux runner (`rekol-ci-runner`) on the maintainer's Mac, authed by a PAT
stored **outside the repo** at `~/.rekol-runner-pat`.

- A self-hosted runner attached to a **public** repo will execute arbitrary code from any
  fork's pull request → RCE on the maintainer's machine. So the runner must be gone *before*
  the repo is public.
- But the hosted-CI config can't be merged *before* the flip either: public repos get
  unlimited hosted minutes, but the account is **out of hosted minutes until then**, so a
  hosted gate would fail every PR while still private.

The resolution is an atomic-ish flip: deregister the runner, swap CI to hosted, then make the
repo public — close together, in this order. The hosted-revert change is **prepared and pushed
as an unmerged branch** (`revert/ci-hosted`) so it can be merged the moment the runner is down.

## Preconditions (do not start until all true)

- [ ] Product gives the explicit go for the public push (coordination channel).
- [ ] QA acceptance pass on `v0.1.0` is recorded.
- [ ] `revert/ci-hosted` branch exists, is pushed, and has been eyeballed (see step 3).
- [ ] You are at a keyboard that can reach the maintainer's Mac (the runner host) and GitHub.

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
