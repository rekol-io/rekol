"""Embedding backends: HashingEmbedder (test double) and SentenceTransformerEmbedder."""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod

import numpy as np

logger = logging.getLogger(__name__)


class BaseEmbedder(ABC):
    """Abstract base class for all embedding backends.

    Subclasses must implement :meth:`dim` and :meth:`embed`. The default
    :meth:`embed_batch` implementation stacks individual :meth:`embed` calls;
    production backends should override it for batching efficiency.
    """

    @property
    @abstractmethod
    def dim(self) -> int:
        """Output vector dimension.

        Returns:
            The number of dimensions in each embedded vector.
        """
        ...

    @abstractmethod
    def embed(self, text: str) -> np.ndarray:
        """Embed a single string into a unit-normalised float32 vector.

        Args:
            text: The input text to embed.

        Returns:
            A float32 ndarray of shape ``(self.dim,)`` with L2 norm ≈ 1.0.
        """
        ...

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        """Embed a list of strings into a 2-D float32 matrix.

        Args:
            texts: List of input strings.

        Returns:
            A float32 ndarray of shape ``(len(texts), self.dim)``.
            Returns an empty matrix of shape ``(0, self.dim)`` for empty input.
        """
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        vecs = [self.embed(t) for t in texts]
        return np.vstack(vecs).astype(np.float32)


class HashingEmbedder(BaseEmbedder):
    """Deterministic feature-hashing embedder for use in tests.

    Produces unit-normalised float32 vectors without loading any ML model,
    making tests fast and dependency-free. NOT semantically meaningful —
    used only as a test double for :class:`SentenceTransformerEmbedder`.

    The algorithm is a feature-hashing variant: for each whitespace token,
    a SHA-256 digest drives slot selection and sign, and the contributions
    are accumulated then L2-normalised.
    """

    def __init__(self, dim: int = 384) -> None:
        """Initialise the hasher.

        Args:
            dim: Output vector dimension. Defaults to 384, matching the
                ``BAAI/bge-small-en-v1.5`` embedding dimension.
        """
        self._dim = dim

    @property
    def dim(self) -> int:
        """Output vector dimension.

        Returns:
            The number of dimensions in each embedded vector.
        """
        return self._dim

    def embed(self, text: str) -> np.ndarray:
        """Embed *text* via feature hashing.

        Args:
            text: Input string. Whitespace-tokenised; case-folded to lowercase.

        Returns:
            Unit-normalised float32 ndarray of shape ``(self.dim,)``.
        """
        vec = np.zeros(self.dim, dtype=np.float32)
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for i in range(0, len(digest), 2):
                slot = int.from_bytes(digest[i : i + 2], "big") % self.dim
                # Use the low bit of the first byte as a deterministic sign.
                sign = 1.0 if (digest[i] & 1) == 0 else -1.0
                vec[slot] += sign
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            # Empty or all-stopword input: fall back to a unit vector at dim 0.
            vec[0] = 1.0
            norm = 1.0
        return (vec / norm).astype(np.float32)


class SentenceTransformerEmbedder(BaseEmbedder):
    """Wraps a sentence-transformers model (local, CPU-friendly).

    The ``sentence_transformers`` import is deferred to ``__init__`` so that
    unit-test modules that import only :class:`HashingEmbedder` never pay
    the torch/transformers startup cost.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        """Load the sentence-transformers model.

        Args:
            model_name: HuggingFace model identifier. Defaults to
                ``"BAAI/bge-small-en-v1.5"`` (130 MB, 384-dim).

        Raises:
            ImportError: If ``sentence-transformers`` is not installed.
        """
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        # Offline-first load. rekol is local-first and caches this model at
        # install, so the steady state needs NO network. Plain
        # ``SentenceTransformer(model_name)`` instead runs an online HuggingFace
        # freshness HEAD on every construction (every `rekol search`/index/etc.),
        # which breaks under air-gap / egress limits / corporate TLS interception
        # — and worse, on failure sentence-transformers can silently build a
        # different mean-pooling model, degrading embeddings without an error.
        # ``local_files_only=True`` uses the cache and makes zero network calls.
        try:
            self._model = SentenceTransformer(model_name, local_files_only=True)
        except OSError:
            # Not in the local cache yet (fresh install / changed model). This is
            # the one legitimate time to reach the network; do the one-time fetch
            # and log it so the network use is visible, not surprising. A genuinely
            # broken cache raises a non-OSError and propagates loudly rather than
            # silently rebuilding a wrong model.
            logger.info(
                "embedding model %r not in local cache; fetching from HuggingFace "
                "(one-time, requires network)…",
                model_name,
            )
            self._model = SentenceTransformer(model_name)
        dim = self._model.get_sentence_embedding_dimension()
        if dim is None:
            raise ValueError(
                f"sentence-transformers model {model_name!r} reported no embedding dimension"
            )
        self._dim = int(dim)

    @property
    def dim(self) -> int:
        """Output vector dimension.

        Returns:
            The number of dimensions in each embedded vector.
        """
        return self._dim

    def embed(self, text: str) -> np.ndarray:
        """Embed a single string using the loaded transformer model.

        Args:
            text: Input string.

        Returns:
            Unit-normalised float32 ndarray of shape ``(self.dim,)``.
        """
        vec = self._model.encode(text, normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        """Embed a list of strings with batched inference.

        Overrides the base class to use the model's native batching for
        better GPU/CPU throughput.

        Args:
            texts: List of input strings.

        Returns:
            Float32 ndarray of shape ``(len(texts), self.dim)``.
        """
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        mat = self._model.encode(texts, normalize_embeddings=True, batch_size=32)
        return np.asarray(mat, dtype=np.float32)


def get_embedder(name: str) -> BaseEmbedder:
    """Factory that returns an embedder by name.

    Args:
        name: One of:
            - ``"test-hashing"`` — returns :class:`HashingEmbedder` (no ML model).
            - ``"BAAI/bge-small-en-v1.5"`` or ``"bge-small-en-v1.5"`` or
              ``"default"`` — returns :class:`SentenceTransformerEmbedder`
              with ``BAAI/bge-small-en-v1.5``.
            - Any other ``"BAAI/..."`` string — passed directly to
              :class:`SentenceTransformerEmbedder`.

    Returns:
        A :class:`BaseEmbedder` instance.

    Raises:
        ValueError: If *name* does not match any known embedder.
    """
    if name == "test-hashing":
        return HashingEmbedder(dim=384)
    if name.startswith("BAAI/") or name in ("default", "bge-small-en-v1.5"):
        model = "BAAI/bge-small-en-v1.5" if name in ("default", "bge-small-en-v1.5") else name
        return SentenceTransformerEmbedder(model_name=model)
    raise ValueError(f"Unknown embedder: {name!r}")
