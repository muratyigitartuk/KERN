from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Generates embeddings using llama-cpp-python (scoped to embedding only)."""

    def __init__(self, model_path: str | None = None) -> None:
        self._model = None
        self._dimensions: int = 0
        if model_path:
            try:
                from llama_cpp import Llama

                self._model = Llama(
                    model_path=model_path,
                    embedding=True,
                    n_ctx=512,
                    verbose=False,
                )
                # Probe dimensions with a test embedding
                test = self._model.embed("test")
                if isinstance(test, list) and test:
                    if isinstance(test[0], list):
                        self._dimensions = len(test[0])
                    else:
                        self._dimensions = len(test)
            except Exception as exc:
                logger.debug("embedding model loading failed: %s", exc)
                self._model = None

    @property
    def available(self) -> bool:
        return self._model is not None and self._dimensions > 0

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, text: str) -> list[float]:
        if not self.available:
            return []
        result = self._model.embed(text)
        if isinstance(result, list) and result:
            if isinstance(result[0], list):
                return result[0]
            return result
        return []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self.available:
            return [[] for _ in texts]
        results = []
        for text in texts:
            results.append(self.embed(text))
        return results
