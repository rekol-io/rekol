"""Coverage report: indexed-vs-discoverable across include_dirs (T8 #63 scope 4).

Answers the spec's adaptive-close question — "Z of ~Y discoverable files indexed
(~C%)" — for the include-scope surface. The numbers are deliberately honest and
cheap:

  * **discoverable (Y)** = every in-scope (deny/junk-filtered) text file on disk
    across all configured ``include_dirs``. This is what *could* be indexed.
  * **indexed (Z)** = the in-scope files belonging to dirs that are *current* —
    a dir whose scoped-manifest hash matches its cached conversion marker, i.e.
    its transcripts are in the index reflecting exactly this fileset. A dir with
    a new/edited/removed file (stale marker) or never converted (no marker)
    contributes to discoverable but not indexed, so the % honestly shows
    "pending re-index" until the next ``session-index`` run catches it up.
  * **percent (C)** = ``100 * Z / Y`` (0 when there is nothing discoverable, no
    divide-by-zero).

After a successful ``session-index`` run with no concurrent edits, every dir is
current and coverage reads 100%.
"""

from __future__ import annotations

from dataclasses import dataclass

from rekol.config import Config
from rekol.include_indexer import dir_is_current, scoped_file_count


@dataclass(frozen=True)
class CoverageReport:
    """Indexed-vs-discoverable counts for the configured include-scope.

    ``percent`` is precomputed (0.0 when nothing is discoverable) so callers
    never re-derive it inconsistently.
    """

    discoverable: int
    indexed: int
    percent: float

    def summary(self) -> str:
        """Render the spec's close line: "Z of ~Y discoverable files indexed (~C%)"."""
        return (
            f"{self.indexed} of ~{self.discoverable} discoverable files indexed "
            f"(~{self.percent:.0f}%)"
        )


def compute_coverage(cfg: Config) -> CoverageReport:
    """Compute include-scope coverage from config + the cached conversion markers.

    Pure read: walks each ``include_dirs`` entry's in-scope fileset and checks its
    marker. No conversion, no writes — safe to call from ``doctor`` / a standalone
    ``coverage`` command. De-duped dirs come from ``resolved_include_dirs`` so a
    dir listed twice is not double-counted.
    """
    discoverable = 0
    indexed = 0
    for include_dir in cfg.resolved_include_dirs():
        count = scoped_file_count(cfg, include_dir)
        discoverable += count
        if dir_is_current(cfg, include_dir):
            indexed += count
    percent = (100.0 * indexed / discoverable) if discoverable else 0.0
    return CoverageReport(discoverable=discoverable, indexed=indexed, percent=percent)


__all__ = ["CoverageReport", "compute_coverage"]
