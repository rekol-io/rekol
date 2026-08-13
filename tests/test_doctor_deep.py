"""Tests for ``rekol doctor --deep`` (the post-install acceptance probes).

The deep probes catch the silent-degradation class a clean install can hide:
a model that fails to load and falls back to a different (mean-pooling) model
keeps the same recorded identity but produces meaningless vectors. Fake
embedders drive the semantic/runtime cases deterministically; the end-to-end
recall probe runs against a real HashingEmbedder-built index.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from click.testing import CliRunner

from rekol.cli_doctor import Status, _check_deep, run_doctor
from rekol.cli_doctor import main as doctor_main
from rekol.config import load_config
from rekol.embeddings import BaseEmbedder, HashingEmbedder
from rekol.indexer import Indexer
from rekol.store import IndexStore


def _home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / "topics").mkdir(parents=True)
    (home / "topics" / "deploy.md").write_text(
        "---\nname: Deploy\ndescription: how we ship\ntype: topic\n---\n\n"
        "# Deploy\n\nWe always deploy via docker compose pull on the build box.\n"
    )
    # Sandbox claude_projects_dir. Without it the default resolves to the REAL
    # ~/.claude/projects, so these tests read the developer's actual transcript
    # store — harmless while "no session index" was graded INFO, but it means the
    # fixture was never isolated. #165 made the grade depend on whether
    # transcripts exist, which turned that latent leak into a failure.
    projects = tmp_path / "claude-projects"
    projects.mkdir(parents=True, exist_ok=True)
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\nclaude_projects_dir: {projects}\n"
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    return home


def _build_index() -> None:
    cfg = load_config()
    emb = HashingEmbedder(dim=384)
    store = IndexStore(
        db_path=cfg.index_db_path, dim=emb.dim, use_sqlite_vec=False, embedding_model="test-hashing"
    )
    store.init_schema()
    Indexer(
        memory_root=cfg.memory_home, store=store, embedder=emb, index_dir=cfg.index_dir
    ).rebuild()
    store.close()


def _finding(findings, label):
    return next(f for f in findings if f.label == label)


class _SeparatingEmbedder(BaseEmbedder):
    """Healthy model: related shares an axis with base, unrelated is orthogonal."""

    @property
    def dim(self) -> int:
        return 384

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(384, dtype=np.float32)
        v[1 if ("basalt" in text or "volcanic" in text) else 0] = 1.0
        return v


class _CollapsedEmbedder(BaseEmbedder):
    """Degraded model: every input maps to the same vector (no separation)."""

    @property
    def dim(self) -> int:
        return 384

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(384, dtype=np.float32)
        v[0] = 1.0
        return v


class _BrokenEmbedder(BaseEmbedder):
    """Model that fails to run at all (e.g. failed load)."""

    @property
    def dim(self) -> int:
        return 384

    def embed(self, text: str) -> np.ndarray:
        raise RuntimeError("model failed to load")


class _NumpyAbiEmbedder(BaseEmbedder):
    """Intel-mac torch built against NumPy 1.x, run under NumPy 2.x."""

    @property
    def dim(self) -> int:
        return 384

    def embed(self, text: str) -> np.ndarray:
        raise ImportError(
            "A module that was compiled using NumPy 1.x cannot be run in NumPy 2.4.6 … "
            "_ARRAY_API not found"
        )


def test_deep_numpy_abi_crash_gives_intel_mac_remedy(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    f = _finding(_check_deep(load_config(), _NumpyAbiEmbedder()), "embedding runtime")
    assert f.status is Status.PROBLEM
    assert f.remedy is not None and "numpy<2" in f.remedy


def test_deep_semantic_ok_with_a_separating_model(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    findings = _check_deep(load_config(), _SeparatingEmbedder())
    assert _finding(findings, "embedding semantics").status is Status.OK


def test_deep_semantic_problem_when_separation_collapses(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    findings = _check_deep(load_config(), _CollapsedEmbedder())
    f = _finding(findings, "embedding semantics")
    assert f.status is Status.PROBLEM
    assert "separation collapsed" in f.detail


def test_deep_runtime_problem_when_embed_raises(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    findings = _check_deep(load_config(), _BrokenEmbedder())
    f = _finding(findings, "embedding runtime")
    assert f.status is Status.PROBLEM
    # A broken embedder short-circuits — no recall probe is attempted.
    assert not any(x.label == "recall probe" for x in findings)


def test_deep_recall_probe_finds_known_chunk_end_to_end(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _build_index()
    # HashingEmbedder is deterministic, so a chunk's own text retrieves itself.
    findings = _check_deep(load_config(), HashingEmbedder(dim=384))
    assert _finding(findings, "recall probe").status is Status.OK


def test_deep_recall_probe_info_when_no_index(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)  # no index built
    findings = _check_deep(load_config(), _SeparatingEmbedder())
    assert _finding(findings, "recall probe").status is Status.INFO


def test_doctor_deep_flag_runs_probes_and_exits_zero_when_healthy(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _build_index()
    # Patch the embedder factory so the deep run uses the separating fake (so the
    # semantic probe passes for the deterministic-but-non-semantic test index).
    import rekol.cli_doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "get_embedder", lambda _name: _SeparatingEmbedder())
    result = CliRunner().invoke(doctor_main, ["--deep"])
    assert result.exit_code == 0, result.output
    assert "embedding semantics" in result.output


def test_run_doctor_without_deep_omits_probes(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _build_index()
    report = run_doctor(load_config(), HashingEmbedder(dim=384))
    assert not any(f.label in ("embedding semantics", "recall probe") for f in report.findings)
