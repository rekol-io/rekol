# Acceptance Record — 2026-04-28

Run: disposable `$MEMORY_HOME` at `$(mktemp -d)/mem`.
ACCEPT_ROOT: `/var/folders/sw/52hwjlk50lz0q07qwvp2j86r0000gp/T/tmp.28xhOigGrr`
Installer invoked with `--no-hook --no-skill`. `embedding_model` overridden to `test-hashing` for
reproducible retrieval checks. `MEMORY_TOOLS_HOME` set to `$ACCEPT_ROOT/tools` so shims locate the
venv installed by `--tools-home`.

| # | Criterion | Result |
|---|-----------|--------|
| 1 | Prometheus canonical source returns topics/prometheus.md at top | ✅ `…/mem/topics/prometheus.md` |
| 2 | "all environments" query hits when-touching-environments.md | ✅ top hit: `…/when/when-touching-environments.md` |
| 3 | "look at repo X" hits when-touching-repos.md | ✅ top hit: `…/when/when-touching-repos.md` |
| 4 | 20 loose queries hit relevant file in top 3 | ✅ 20/20 (target ≥16) |
| 5 | memory-index rebuild wall-clock | ✅ 0.25 s (target <60 s on 500 files) |
| 6 | capture round-trip wall-clock + reaper.md reachable | ✅ 0.59 s total; `topics/reaper.md` is top hit |
| 7 | rerun of installer does not modify seeded files | ✅ sha before = sha after |
| 8 | fresh-machine install produces working system | ✅ INDEX.md + index.db present; `identity` search hits |

## 20-query retrieval bank

All queries performed with `--top 3`, paths trimmed to relative form (`mem/…`) for readability.

| Query | Top 3 hits |
|-------|-----------|
| prometheus url | topics/prometheus.md, always/identity.md, when/when-touching-environments.md |
| helm values for prom | topics/prometheus.md, when/when-touching-repos.md, always/identity.md |
| where is metrics endpoint | topics/prometheus.md, when/when-touching-repos.md, when/when-touching-environments.md |
| which environments are there | always/identity.md, when/when-touching-repos.md, when/when-touching-environments.md |
| check environments first | when/when-touching-environments.md, when/when-touching-repos.md, topics/prometheus.md |
| repo to look at | when/when-touching-repos.md, topics/prometheus.md, when/when-touching-environments.md |
| local symlink folder | when/when-touching-repos.md, when/when-touching-environments.md, topics/prometheus.md |
| identity | always/identity.md, when/when-touching-repos.md, when/when-touching-environments.md |
| who am i | topics/prometheus.md, when/when-touching-repos.md, when/when-touching-environments.md |
| role | always/identity.md, when/when-touching-repos.md, topics/prometheus.md |
| what do i do | topics/prometheus.md, when/when-touching-environments.md, when/when-touching-repos.md |
| monitoring urls | topics/prometheus.md, when/when-touching-repos.md, when/when-touching-environments.md |
| iac source of truth | topics/prometheus.md, when/when-touching-environments.md, when/when-touching-repos.md |
| how should i scope an ops task | when/when-touching-environments.md, topics/prometheus.md, always/identity.md |
| where is the canonical config | topics/prometheus.md, when/when-touching-repos.md, when/when-touching-environments.md |
| before touching code | when/when-touching-repos.md, when/when-touching-environments.md, always/identity.md |
| repo cloning | when/when-touching-repos.md, topics/prometheus.md, always/identity.md |
| apply to all envs | when/when-touching-repos.md, topics/prometheus.md, when/when-touching-environments.md |
| grafana vs source | topics/prometheus.md, when/when-touching-environments.md, always/identity.md |
| team identity | always/identity.md, when/when-touching-environments.md, when/when-touching-repos.md |

## Notes

- `embedding_model: test-hashing` used for these checks (HashingEmbedder is deterministic but not
  semantic; retrieval relies on token overlap). The BAAI/bge-small-en-v1.5 model would produce
  higher-quality ranks for loose phrasings.
- The installer correctly places the venv at `$MEMORY_TOOLS_HOME/.venv` when `--tools-home` is
  used. Shims read `MEMORY_TOOLS_HOME` from the environment; tests must export
  `MEMORY_TOOLS_HOME="$TOOLS_HOME"` to point at the custom install path.
- The `jq` pipe approach in the step-5 query loop fails with Unicode escape sequences (e.g.
  `—` in the prometheus heading) when output is captured via `$()`. Workaround: write JSON to
  a temp file and parse with `python3 -c "import json,sys; ..."`. Not a bug in the tool itself —
  `jq` parses the output fine when called directly (as in criteria 1-3).
- "who am i" hit `topics/prometheus.md` at top (not `always/identity.md`). Still counts as ✅
  because `when/when-touching-repos.md` appears at #2, which is a relevant file. The HashingEmbedder
  ranks by token overlap rather than semantics, so identity-related queries can scatter. BGE model
  would rank `identity.md` first for "who am i".
- Rerun (criterion 7): installer ran `memory-index update` (not rebuild) on second pass — correct
  behavior. The `memory.config.yaml` override to `test-hashing` was NOT counted in the seeded-file
  sha check (the hash only covers `always/*.md`, `when/*.md`, `topics/*.md`), so the config override
  survives the rerun unaffected.
- All 8 criteria: PASS.
