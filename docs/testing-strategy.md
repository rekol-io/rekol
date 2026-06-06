# Testing & releases

How rekol is tested and versioned. Relates to [#27](https://github.com/rekol-io/rekol/issues/27) (`rekol update`) and [#28](https://github.com/rekol-io/rekol/issues/28) (release versioning).

## Running the tests

The suite is the merge gate on every PR:

```
ruff check .
ruff format --check .
mypy src/rekol
pytest
bats tests/test_install.bats
```

Tests are **hermetic** — they clear `REKOL_HOME`/`MEMORY_HOME` and point all derived
state (index cache, archive) at temp dirs, so they never touch your real memory.

## Versioning & release channels (`0.x.y`)

- **Edge (`y`)** — every merged PR cuts a point release; for picking up a specific
  non-critical fix early.
- **Stable (`x`)** — the vetted release; what `rekol update` recommends and defaults to.

SemVer `0.x` during alpha (pre-1.0); a GitHub Release per tag.

## Retrieval-quality (behavioral) testing

The suite above proves the machinery works — search returns ranked results. Whether
retrieval is actually *good* (the right memory surfacing at the right moment) is a
separate question, evaluated by an agent-in-the-loop harness run against real memory.
What it checks (the methodology is open):

- **retrieval relevance** — does the right memory rank at the top for a query;
- **proactive surfacing** — does the right memory surface *without the agent knowing to
  search*;
- **ranking quality** — recency/layer boosts behave; stale facts decay;
- **no-regression** — known query→answer pairs keep working release over release.

Because it exercises real, personal memory data, that harness runs **locally** and its
fixtures are private — but the methodology above is open, and any deterministic gap it
finds becomes a normal test in this repo.
