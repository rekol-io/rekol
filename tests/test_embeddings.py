import sys
import types

import numpy as np
import pytest

from rekol.embeddings import (
    BaseEmbedder,
    HashingEmbedder,
    SentenceTransformerEmbedder,
    get_embedder,
)


class _FakeModel:
    """Stand-in for a loaded sentence-transformers model."""

    def get_sentence_embedding_dimension(self) -> int:
        return 384


def _install_fake_sentence_transformers(monkeypatch, loader) -> list[dict]:
    """Inject a fake ``sentence_transformers`` module so the deferred import in
    ``SentenceTransformerEmbedder.__init__`` picks up ``loader`` instead of the
    real (torch-backed) library. Returns the list that records each call's kwargs.

    ``loader(model_name, **kwargs)`` is invoked on every ``SentenceTransformer(...)``
    construction; tests use the recorded kwargs to assert offline-first behavior.
    """
    calls: list[dict] = []

    def _spy(model_name, **kwargs):
        calls.append(kwargs)
        return loader(model_name, **kwargs)

    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = _spy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    return calls


def test_sentence_transformer_loads_offline_first(monkeypatch) -> None:
    """Steady state (model cached): load with local_files_only=True, no online retry."""
    calls = _install_fake_sentence_transformers(monkeypatch, lambda name, **kw: _FakeModel())

    emb = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")

    assert emb.dim == 384
    assert len(calls) == 1, "a cached model must load in exactly one call (no online retry)"
    assert calls[0].get("local_files_only") is True


def test_sentence_transformer_falls_back_to_online_when_uncached(monkeypatch) -> None:
    """Not cached yet: the offline attempt OSErrors, then exactly one online-permitted retry."""

    def loader(name, **kwargs):
        if kwargs.get("local_files_only"):
            raise OSError("model not found in local cache")
        return _FakeModel()

    calls = _install_fake_sentence_transformers(monkeypatch, loader)

    emb = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")

    assert emb.dim == 384
    assert len(calls) == 2, "expected one offline attempt + one online fallback"
    assert calls[0].get("local_files_only") is True
    assert "local_files_only" not in calls[1], (
        "fallback must allow the network (no local_files_only)"
    )


def test_sentence_transformer_non_oserror_propagates_no_silent_fallback(monkeypatch) -> None:
    """A non-OSError (e.g. corrupt cache) propagates loudly — never a silent online rebuild."""

    def loader(name, **kwargs):
        raise RuntimeError("corrupt model cache")

    calls = _install_fake_sentence_transformers(monkeypatch, loader)

    with pytest.raises(RuntimeError, match="corrupt model cache"):
        SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")

    assert len(calls) == 1, "a non-OSError must NOT trigger the online fallback"


def test_hashing_embedder_returns_correct_shape() -> None:
    e = HashingEmbedder(dim=384)
    vec = e.embed("hello world")
    assert vec.shape == (384,)
    assert vec.dtype == np.float32


def test_hashing_embedder_is_deterministic() -> None:
    e = HashingEmbedder(dim=384)
    v1 = e.embed("the quick brown fox")
    v2 = e.embed("the quick brown fox")
    assert np.array_equal(v1, v2)


def test_hashing_embedder_different_text_different_vec() -> None:
    e = HashingEmbedder(dim=384)
    v1 = e.embed("prometheus url")
    v2 = e.embed("reaper schedule")
    assert not np.array_equal(v1, v2)


def test_hashing_embedder_unit_normalized() -> None:
    e = HashingEmbedder(dim=384)
    vec = e.embed("anything")
    norm = np.linalg.norm(vec)
    assert abs(norm - 1.0) < 1e-5


def test_embed_batch_returns_matrix() -> None:
    e = HashingEmbedder(dim=384)
    mat = e.embed_batch(["a", "b", "c"])
    assert mat.shape == (3, 384)


def test_get_embedder_returns_hashing_for_test_model() -> None:
    emb = get_embedder("test-hashing")
    assert isinstance(emb, HashingEmbedder)
    assert isinstance(emb, BaseEmbedder)


def test_get_embedder_raises_on_unknown_name() -> None:
    with pytest.raises(ValueError):
        get_embedder("not-a-real-model")


def test_embed_batch_empty_returns_empty_matrix() -> None:
    e = HashingEmbedder(dim=384)
    mat = e.embed_batch([])
    assert mat.shape == (0, 384)
    assert mat.dtype == np.float32


def test_base_embedder_cannot_be_instantiated() -> None:
    """Subclasses that don't implement both `dim` and `embed` must fail to instantiate."""

    class IncompleteEmbedder(BaseEmbedder):
        @property
        def dim(self) -> int:
            return 8

        # Missing embed() — should fail

    with pytest.raises(TypeError):
        IncompleteEmbedder()  # type: ignore[abstract]
