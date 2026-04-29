import numpy as np
import pytest

from memory_tools.embeddings import (
    BaseEmbedder,
    HashingEmbedder,
    get_embedder,
)


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
