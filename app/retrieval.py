from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import struct
from collections import Counter
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import numpy as np

from app.config import settings
from app.memory import MemoryRepository
from app.platform import PlatformStore
from app.types import MemoryScope, RetrievalHit, RetrievalStatusSnapshot

if TYPE_CHECKING:
    from app.embeddings import EmbeddingService

logger = logging.getLogger(__name__)


class RetrievalService:
    BACKEND_NAME = "local_tfidf"
    TOKEN_PATTERN = re.compile(r"[0-9A-Za-zÄÖÜäöüß]+")

    QUERY_SYNONYMS: dict[str, tuple[str, ...]] = {
        "angebot": ("offer",),
        "rechnung": ("invoice",),
        "rechnungen": ("invoice",),
        "zielbetrag": ("target", "amount"),
        "betrag": ("amount", "total"),
        "summe": ("amount", "total"),
        "faellig": ("due",),
        "fällig": ("due",),
        "faelligkeit": ("due", "date"),
        "fälligkeit": ("due", "date"),
        "lieferant": ("contact", "supplier"),
        "ust": ("vat",),
        "ustidnr": ("vat", "id"),
        "ustid": ("vat", "id"),
    }

    def __init__(
        self,
        memory: MemoryRepository,
        platform: PlatformStore | None = None,
        profile_slug: str | None = None,
        embedding_service: "EmbeddingService | None" = None,
    ) -> None:
        self.memory = memory
        self.platform = platform
        self.profile_slug = profile_slug or getattr(memory, "profile_slug", None)
        self._embedding_service = embedding_service
        self._vec_loaded = False
        self._candidate_cache: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._embedding_cache: dict[str, dict[str, np.ndarray]] = {}
        self._last_query = ""
        self._last_hits: list[RetrievalHit] = []
        self._status = RetrievalStatusSnapshot(
            ready=not settings.rag_enabled,
            backend=self.BACKEND_NAME if settings.rag_enabled else "lexical",
            index_health="missing" if settings.rag_enabled else "disabled",
            reason="Local TF-IDF index not built yet." if settings.rag_enabled else "Lexical fallback active.",
        )

    @property
    def status(self) -> RetrievalStatusSnapshot:
        return self._status

    @property
    def last_query(self) -> str:
        return self._last_query

    @property
    def last_hits(self) -> list[RetrievalHit]:
        return list(self._last_hits)

    @property
    def vec_available(self) -> bool:
        return (
            settings.vec_enabled
            and self._embedding_service is not None
            and self._embedding_service.available
            and self._vec_loaded
        )

    def _ensure_vec(self) -> bool:
        if self._vec_loaded:
            return True
        if not settings.vec_enabled or self._embedding_service is None or not self._embedding_service.available:
            return False
        try:
            from app.database import load_sqlite_vec, ensure_vec_table

            if load_sqlite_vec(self.memory.connection):
                ensure_vec_table(self.memory.connection, self._embedding_service.dimensions)
                self._vec_loaded = True
                return True
        except Exception as exc:
            logger.debug("Failed to load sqlite-vec extension: %s", exc, exc_info=True)
        return False

    def retrieve(self, query: str, scope: MemoryScope = "profile", limit: int = 8) -> list[RetrievalHit]:
        query = query.strip()
        self._last_query = query
        if not query or scope in {"off", "session"}:
            self._last_hits = []
            return []

        if self.vec_available or self._ensure_vec():
            hits = self._vec_retrieve(query, scope=scope, limit=limit)
        elif settings.rag_enabled:
            hits = self._indexed_retrieve(query, scope=scope, limit=limit)
        else:
            hits = self._lexical_retrieve(query, scope=scope, limit=limit)
        self._last_hits = hits
        if self.platform and self.profile_slug:
            self.platform.record_audit(
                "retrieval",
                "search",
                "success",
                "Retrieved knowledge hits.",
                profile_slug=self.profile_slug,
                details={"scope": scope, "hits": len(hits), "backend": self._status.backend, "query_length": len(query)},
            )
        return hits

    def retrieve_from_documents(self, query: str, document_ids: list[str], limit: int = 8) -> list[RetrievalHit]:
        """Retrieve chunks scoped to specific document IDs."""
        query = query.strip()
        if not query or not document_ids:
            return []
        chunks = self.memory.list_document_chunks_for_documents(document_ids, include_archived=True)
        query_terms = self._tokenize(query)
        hits: list[RetrievalHit] = []
        for chunk in chunks:
            haystack = f"{chunk['title']} {chunk['text']}".lower()
            overlap = sum(1 for term in query_terms if term in haystack)
            if overlap <= 0:
                continue
            hits.append(
                RetrievalHit(
                    source_type=str(chunk["source_type"]),
                    source_id=str(chunk["source_id"]),
                    score=1.0 + min(overlap, 6) * 0.15,
                    text=str(chunk["text"]),
                    metadata={
                        "title": str(chunk["title"]),
                        "document_id": str(chunk["source_id"]),
                        "chunk_index": int(chunk["chunk_index"]),
                        "file_path": chunk.get("file_path"),
                        "classification": chunk.get("classification"),
                    },
                    provenance={
                        "source_type": str(chunk["source_type"]),
                        "profile_slug": self.profile_slug,
                        "scope": "documents",
                        "classification": chunk.get("classification"),
                    },
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.metadata.get("title", "")))
        return hits[:limit]

    def rebuild_index(self, scope: MemoryScope = "profile_plus_archive") -> RetrievalStatusSnapshot:
        return self._rebuild_index(scope=scope)

    def recover_jobs(self) -> None:
        if not (self.platform and self.profile_slug):
            return
        for job in self.platform.list_jobs(self.profile_slug, limit=20):
            if job.job_type != "retrieval_reindex" or not job.recoverable:
                continue
            scope = str(job.payload.get("scope", "profile_plus_archive") or "profile_plus_archive")
            try:
                self.platform.update_job(
                    job.id,
                    status="running",
                    recoverable=True,
                    detail="Resuming retrieval index rebuild.",
                    checkpoint_stage=job.checkpoint_stage or "resume",
                    progress=0.1,
                )
                self._rebuild_index(scope=scope, existing_job_id=job.id)
                self.platform.update_job(
                    job.id,
                    status="completed",
                    recoverable=False,
                    detail="Retrieval index rebuild recovered.",
                    checkpoint_stage="recovered",
                    progress=1.0,
                    result={"scope": scope},
                )
            except Exception as exc:
                self.platform.update_job(
                    job.id,
                    status="failed",
                    recoverable=False,
                    detail=str(exc),
                    checkpoint_stage="failed",
                    error_code="retrieval_recovery_failed",
                    error_message=str(exc),
                    result={"scope": scope},
                )

    def _rebuild_index(self, scope: MemoryScope, existing_job_id: str | None = None) -> RetrievalStatusSnapshot:
        self._candidate_cache.clear()
        self._embedding_cache.clear()
        if not settings.rag_enabled:
            self._status = RetrievalStatusSnapshot(
                ready=True,
                backend="lexical",
                index_health="disabled",
                reason="Lexical fallback active.",
            )
            return self._status

        job = None
        checkpoint_at = datetime.now(timezone.utc).isoformat()
        expected_model = settings.rag_embed_model or self.BACKEND_NAME
        try:
            if self.platform and self.profile_slug and existing_job_id:
                job = next((item for item in self.platform.list_jobs(self.profile_slug, limit=50) if item.id == existing_job_id), None)
            elif self.platform and self.profile_slug:
                job = self.platform.create_job(
                    "retrieval_reindex",
                    "Rebuild retrieval index",
                    profile_slug=self.profile_slug,
                    detail="Building local retrieval index.",
                    payload={"scope": scope, "backend": self.BACKEND_NAME, "embed_model": expected_model},
                )
            if self.platform and job:
                self.platform.update_job(job.id, status="running", detail="Building local retrieval index.", progress=0.05)
                self.platform.update_checkpoint(job.id, "retrieval_reindex_started", {"scope": scope, "backend": self.BACKEND_NAME})
            self._status = RetrievalStatusSnapshot(
                ready=False,
                backend=self.BACKEND_NAME,
                index_health="building",
                reason="Rebuilding local TF-IDF retrieval index.",
            )
            sources = self._build_candidates(scope=scope)
            vectors, vocabulary, idf = self._fit_tfidf_index(sources)
            if self.platform and job:
                self.platform.update_checkpoint(job.id, "retrieval_reindex_candidates", {"item_count": len(sources), "scope": scope, "vocabulary_size": len(vocabulary)})
            connection = self.memory.connection
            connection.execute("DELETE FROM memory_embeddings WHERE content_type = ?", ("retrieval",))
            for content_id, vector in vectors.items():
                connection.execute(
                    """
                    INSERT INTO memory_embeddings (content_type, content_id, embedding_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("retrieval", content_id, json.dumps(vector), datetime.now(timezone.utc).isoformat()),
                )
            built_at = datetime.now(timezone.utc).isoformat()
            metadata_map = {
                "backend": self.BACKEND_NAME,
                "embed_model": expected_model,
                "index_version": settings.rag_index_version,
                "built_at": built_at,
                "item_count": str(len(sources)),
                "vocabulary_json": json.dumps(vocabulary),
                "idf_json": json.dumps(idf),
            }
            for key, value in metadata_map.items():
                connection.execute(
                    """
                    INSERT INTO key_value_store (bucket, key, value)
                    VALUES ('retrieval_index', ?, ?)
                    ON CONFLICT(bucket, key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
            connection.commit()
            if self.platform and job:
                self.platform.update_checkpoint(job.id, "retrieval_reindex_completed", {"item_count": len(sources), "built_at": built_at, "vocabulary_size": len(vocabulary)})
                self.platform.update_job(
                    job.id,
                    status="completed",
                    detail=f"Indexed {len(sources)} retrieval item(s).",
                    progress=1.0,
                    result={"item_count": len(sources), "built_at": built_at, "vocabulary_size": len(vocabulary)},
                    checkpoint_stage="retrieval_reindex_completed",
                )
                self.platform.record_audit(
                    "retrieval",
                    "reindex",
                    "success",
                    f"Rebuilt retrieval index with {len(sources)} item(s).",
                    profile_slug=self.profile_slug,
                    details={"scope": scope, "item_count": len(sources), "started_at": checkpoint_at, "backend": self.BACKEND_NAME},
                )
            self._status = RetrievalStatusSnapshot(
                ready=True,
                backend=self.BACKEND_NAME,
                index_health="healthy",
                reason=f"Indexed {len(sources)} retrieval items with local TF-IDF.",
                last_index_build_at=datetime.fromisoformat(built_at),
            )
            return self._status
        except Exception as exc:
            try:
                self.memory.connection.rollback()
            except Exception as exc:
                logger.debug("Rollback after reindex failure failed: %s", exc, exc_info=True)
            if self.platform and job:
                self.platform.update_job(
                    job.id,
                    status="failed",
                    detail=str(exc),
                    error_code="retrieval_reindex_failed",
                    error_message=str(exc),
                    checkpoint_stage="retrieval_reindex_failed",
                )
                self.platform.record_audit("retrieval", "reindex", "failure", str(exc), profile_slug=self.profile_slug, details={"scope": scope, "started_at": checkpoint_at})
            self._status = RetrievalStatusSnapshot(
                ready=False,
                backend=self.BACKEND_NAME,
                index_health="missing",
                reason=f"Retrieval index rebuild failed: {exc}",
            )
            raise

    def _indexed_retrieve(self, query: str, scope: MemoryScope, limit: int) -> list[RetrievalHit]:
        built_at = self.memory.get_index_metadata("built_at", "") or "unbuilt"
        candidates = self._cached_candidates(scope)
        expected_model = settings.rag_embed_model or self.BACKEND_NAME
        built_version = self.memory.get_index_metadata("index_version", "")
        built_model = self.memory.get_index_metadata("embed_model", "")
        built_backend = self.memory.get_index_metadata("backend", "")
        vocabulary = self._load_json_metadata("vocabulary_json")
        idf = self._load_json_metadata("idf_json")
        embeddings = self.memory.list_embeddings("retrieval")
        if (
            not embeddings
            or built_version != settings.rag_index_version
            or built_model != expected_model
            or built_backend != self.BACKEND_NAME
            or not isinstance(vocabulary, list)
            or not isinstance(idf, dict)
        ):
            try:
                self._rebuild_index(scope="profile_plus_archive")
            except Exception as exc:
                logger.debug("Retrieval index rebuild failed: %s", exc, exc_info=True)
                self._status = RetrievalStatusSnapshot(
                    ready=False,
                    backend=self.BACKEND_NAME,
                    index_health="missing",
                    reason="Retrieval index rebuild failed; lexical fallback active.",
                )
                return self._lexical_retrieve(query, scope=scope, limit=limit)
            embeddings = self.memory.list_embeddings("retrieval")
            vocabulary = self._load_json_metadata("vocabulary_json")
            idf = self._load_json_metadata("idf_json")
            built_at = self.memory.get_index_metadata("built_at", "") or "unbuilt"
            candidates = self._cached_candidates(scope)

        if not embeddings or not isinstance(vocabulary, list) or not isinstance(idf, dict):
            self._status = RetrievalStatusSnapshot(
                ready=False,
                backend=self.BACKEND_NAME,
                index_health="missing",
                reason="Retrieval index is empty.",
            )
            return self._lexical_retrieve(query, scope=scope, limit=limit)

        query_vector = self._vectorize_tfidf(query, vocabulary, idf)
        if not query_vector:
            return self._lexical_retrieve(query, scope=scope, limit=limit)

        embeddings_by_id = self._cached_embeddings(embeddings, built_at)
        scored: list[RetrievalHit] = []
        query_terms = self._tokenize(query)
        for content_id, candidate in candidates.items():
            stored_vector = embeddings_by_id.get(content_id)
            if stored_vector is None:
                continue
            cosine_score = self._cosine(query_vector, stored_vector)
            lexical_bonus = self._lexical_bonus(query_terms, candidate)
            score = cosine_score * 0.9 + lexical_bonus
            if score <= 0:
                continue
            scored.append(
                RetrievalHit(
                    source_type=candidate["source_type"],
                    source_id=candidate["source_id"],
                    score=round(float(score), 4),
                    text=candidate["text"],
                    metadata={
                        "title": candidate["title"],
                        "chunk_id": candidate["content_id"],
                        "chunk_index": candidate["chunk_index"],
                        "backend": self.BACKEND_NAME,
                        "scope": scope,
                        **candidate.get("metadata", {}),
                    },
                    provenance={
                        "source_type": candidate["source_type"],
                        "profile_slug": self.profile_slug,
                        "scope": scope,
                        "classification": candidate.get("metadata", {}).get("classification"),
                        "origin_provenance": candidate.get("metadata", {}).get("provenance"),
                    },
                )
            )
        built_at = self.memory.get_index_metadata("built_at")
        self._status = RetrievalStatusSnapshot(
            ready=True,
            backend=self.BACKEND_NAME,
            index_health="healthy",
            reason="Local TF-IDF retrieval active.",
            last_index_build_at=datetime.fromisoformat(built_at) if built_at else None,
        )
        return sorted(scored, key=lambda item: (-item.score, item.metadata.get("title", "")))[:limit]

    def _vec_retrieve(self, query: str, scope: MemoryScope, limit: int) -> list[RetrievalHit]:
        if self._embedding_service is None or not self._embedding_service.available:
            return self._lexical_retrieve(query, scope=scope, limit=limit)
        query_vec = self._embedding_service.embed(query)
        if not query_vec:
            return self._lexical_retrieve(query, scope=scope, limit=limit)
        try:
            blob = struct.pack(f"{len(query_vec)}f", *query_vec)
            rows = self.memory.connection.execute(
                """
                SELECT content_type, content_id, distance
                FROM vec_embeddings
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
                """,
                (blob, limit * 2),
            ).fetchall()
        except Exception as exc:
            logger.debug("Vec retrieval query failed, falling back to lexical: %s", exc, exc_info=True)
            return self._lexical_retrieve(query, scope=scope, limit=limit)

        candidates = self._cached_candidates(scope)
        hits: list[RetrievalHit] = []
        for row in rows:
            content_id = str(row["content_id"])
            candidate = candidates.get(content_id)
            if candidate is None:
                continue
            distance = float(row["distance"])
            score = max(0.0, 1.0 - distance)
            if score <= 0:
                continue
            hits.append(RetrievalHit(
                source_type=candidate["source_type"],
                source_id=candidate["source_id"],
                score=round(score, 4),
                text=candidate["text"],
                metadata={
                    "title": candidate["title"],
                    "chunk_id": content_id,
                    "chunk_index": candidate["chunk_index"],
                    "backend": "sqlite_vec",
                    "scope": scope,
                    **candidate.get("metadata", {}),
                },
                provenance={
                    "source_type": candidate["source_type"],
                    "profile_slug": self.profile_slug,
                    "scope": scope,
                    "classification": candidate.get("metadata", {}).get("classification"),
                    "origin_provenance": candidate.get("metadata", {}).get("provenance"),
                },
            ))
        self._status = RetrievalStatusSnapshot(
            ready=True,
            backend="local_embedding",
            index_health="healthy",
            reason="sqlite-vec vector retrieval active.",
        )
        return sorted(hits, key=lambda h: (-h.score, h.metadata.get("title", "")))[:limit]

    def rebuild_vec_index(self, scope: MemoryScope = "profile_plus_archive") -> RetrievalStatusSnapshot:
        if not self._ensure_vec() or self._embedding_service is None:
            return self._status
        self._candidate_cache.clear()
        self._embedding_cache.clear()
        candidates = self._build_candidates(scope=scope)
        connection = self.memory.connection
        try:
            connection.execute("DELETE FROM vec_embeddings")
        except Exception as exc:
            logger.debug("Failed to clear vec_embeddings table: %s", exc, exc_info=True)
        texts = {cid: self._indexable_text(c) for cid, c in candidates.items()}
        all_cids = list(texts.keys())
        batch_size = max(1, settings.vec_index_batch_size)
        expected_dimensions = getattr(self._embedding_service, "dimensions", 0)
        for start in range(0, len(all_cids), batch_size):
            batch_cids = all_cids[start : start + batch_size]
            batch_texts = [texts[cid] for cid in batch_cids]
            embeddings = self._embedding_service.embed_batch(batch_texts)
            for cid, vec in zip(batch_cids, embeddings):
                if not vec:
                    continue
                if expected_dimensions and len(vec) != expected_dimensions:
                    logger.warning("Skipping vec index entry for %s due to unexpected embedding length %d", cid, len(vec))
                    continue
                candidate = candidates[cid]
                blob = struct.pack(f"{len(vec)}f", *vec)
                connection.execute(
                    "INSERT INTO vec_embeddings (content_type, content_id, embedding) VALUES (?, ?, ?)",
                    (candidate["source_type"], cid, blob),
                )
        connection.commit()
        self._status = RetrievalStatusSnapshot(
            ready=True,
            backend="local_embedding",
            index_health="healthy",
            reason=f"sqlite-vec index built with {len(all_cids)} items.",
            last_index_build_at=datetime.now(timezone.utc),
        )
        return self._status

    def _lexical_retrieve(self, query: str, scope: MemoryScope, limit: int) -> list[RetrievalHit]:
        query_terms = [term for term in self._tokenize(query) if term]
        hits: list[RetrievalHit] = []

        if scope in {"profile", "profile_plus_archive"}:
            for fact in self.memory.search_facts(query, limit=8):
                lowered = f"{fact.key} {fact.value}".lower()
                score = 1.1 + sum(1 for term in query_terms if term in lowered) * 0.18
                hits.append(
                    RetrievalHit(
                        source_type="memory",
                        source_id=fact.key,
                        score=score,
                        text=f"{fact.key}: {fact.value}",
                        metadata={
                            "title": fact.key,
                            "source": fact.source,
                            "memory_kind": fact.memory_kind,
                            "entity_key": fact.entity_key,
                            "status": fact.status,
                            "provenance": fact.provenance,
                            "backend": "lexical",
                            "scope": scope,
                            "chunk_id": f"memory:{fact.key}",
                        },
                        provenance={
                            "source_type": "memory",
                            "profile_slug": self.profile_slug,
                            "scope": scope,
                            "origin_provenance": fact.provenance,
                        },
                    )
                )

        include_archived = scope == "profile_plus_archive"
        raw_hits = self.memory.search_document_chunks(query, limit=16, include_archived=include_archived)
        aggregated: dict[tuple[str, str], RetrievalHit] = {}
        for hit in raw_hits:
            key = (hit.source_type, hit.source_id)
            lowered = hit.text.lower()
            title = str(hit.metadata.get("title", "document"))
            score = 1.0 + sum(1 for term in query_terms if term in lowered or term in title.lower()) * 0.15
            existing = aggregated.get(key)
            if existing is None or score > existing.score:
                aggregated[key] = RetrievalHit(
                    source_type=hit.source_type,
                    source_id=hit.source_id,
                    score=score,
                    text=hit.text,
                    metadata={
                        **hit.metadata,
                        "backend": "lexical",
                        "scope": scope,
                        "chunk_id": f"{hit.source_type}:{hit.source_id}:{hit.metadata.get('chunk_index', 0)}",
                    },
                    provenance={
                        "source_type": hit.source_type,
                        "profile_slug": self.profile_slug,
                        "scope": scope,
                        "classification": hit.metadata.get("classification"),
                        "origin_provenance": hit.metadata.get("provenance"),
                    },
                )
        hits.extend(aggregated.values())
        self._status = RetrievalStatusSnapshot(
            ready=True,
            backend="lexical",
            index_health="disabled" if not settings.rag_enabled else "missing",
            reason="Lexical retrieval active." if not settings.rag_enabled else "TF-IDF index unavailable; lexical fallback active.",
        )
        return sorted(hits, key=lambda hit: (-hit.score, hit.metadata.get("title", "")))[:limit]

    def _build_candidates(self, scope: MemoryScope) -> dict[str, dict[str, Any]]:
        candidates: dict[str, dict[str, Any]] = {}
        if scope in {"profile", "profile_plus_archive"}:
            for fact in self.memory.list_all_facts():
                if fact.memory_kind == "episodic_turn":
                    continue
                content_id = f"memory:{fact.key}:{hashlib.sha256(fact.value.encode('utf-8')).hexdigest()[:16]}"
                candidates[content_id] = {
                    "content_id": content_id,
                    "source_type": "memory",
                    "source_id": fact.key,
                    "title": fact.key,
                    "chunk_index": 0,
                    "text": f"{fact.key}: {fact.value}",
                    "metadata": {
                        "source": fact.source,
                        "memory_kind": fact.memory_kind,
                        "entity_key": fact.entity_key,
                        "status": fact.status,
                        "provenance": fact.provenance,
                    },
                }
        for chunk in self.memory.list_all_document_chunks(include_archived=scope == "profile_plus_archive"):
            content_id = f"{chunk['source_id']}:{chunk['chunk_index']}"
            candidates[content_id] = {
                "content_id": content_id,
                "source_type": chunk["source_type"],
                "source_id": chunk["source_id"],
                "title": chunk["title"],
                "chunk_index": chunk["chunk_index"],
                "text": chunk["text"],
                "metadata": {
                    "file_path": chunk.get("file_path"),
                    "classification": chunk.get("classification"),
                },
            }
        return candidates

    def _cached_candidates(self, scope: MemoryScope) -> dict[str, dict[str, Any]]:
        signature = self.memory.get_index_metadata("built_at", "") or "unbuilt"
        cache_key = (scope, signature)
        cached = self._candidate_cache.get(cache_key)
        if cached is not None:
            return cached
        candidates = self._build_candidates(scope=scope)
        self._candidate_cache = {cache_key: candidates}
        return candidates

    def _cached_embeddings(self, embeddings: list[dict[str, object]], signature: str) -> dict[str, np.ndarray]:
        cached = self._embedding_cache.get(signature)
        if cached is not None:
            return cached
        cached = {str(row["content_id"]): np.array(row["embedding"], dtype=np.float32) for row in embeddings}
        self._embedding_cache = {signature: cached}
        return cached

    def _fit_tfidf_index(self, candidates: dict[str, dict[str, Any]]) -> tuple[dict[str, list[float]], list[str], dict[str, float]]:
        documents: dict[str, list[str]] = {}
        document_frequency: Counter[str] = Counter()
        for content_id, candidate in candidates.items():
            tokens = self._tokenize(self._indexable_text(candidate))
            documents[content_id] = tokens
            for token in set(tokens):
                document_frequency[token] += 1
        vocabulary = sorted(document_frequency.keys())
        if not vocabulary:
            return {}, [], {}
        total_documents = max(1, len(documents))
        idf = {token: math.log((1.0 + total_documents) / (1.0 + document_frequency[token])) + 1.0 for token in vocabulary}
        token_positions = {token: index for index, token in enumerate(vocabulary)}
        vectors: dict[str, list[float]] = {}
        for content_id, tokens in documents.items():
            vectors[content_id] = self._tfidf_from_tokens(tokens, token_positions, idf)
        return vectors, vocabulary, idf

    def _tfidf_from_tokens(self, tokens: list[str], token_positions: dict[str, int], idf: dict[str, float]) -> list[float]:
        if not tokens or not token_positions:
            return []
        term_counts = Counter(tokens)
        vector = np.zeros(len(token_positions), dtype=np.float32)
        max_count = max(term_counts.values()) if term_counts else 1
        for token, count in term_counts.items():
            position = token_positions.get(token)
            if position is None:
                continue
            term_frequency = count / max_count
            vector[position] = term_frequency * float(idf.get(token, 0.0))
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm
        return vector.astype(np.float32).tolist()

    def _vectorize_tfidf(self, text: str, vocabulary: list[str], idf: dict[str, Any]) -> list[float]:
        token_positions = {token: index for index, token in enumerate(vocabulary)}
        normalized_idf = {str(key): float(value) for key, value in idf.items()}
        return self._tfidf_from_tokens(self._tokenize(text), token_positions, normalized_idf)

    def _lexical_bonus(self, query_terms: list[str], candidate: dict[str, Any]) -> float:
        if not query_terms:
            return 0.0
        haystack = f"{candidate['title']} {candidate['text']}".lower()
        overlap = sum(1 for term in query_terms if term in haystack)
        return min(overlap, 4) * 0.035

    def _indexable_text(self, candidate: dict[str, Any]) -> str:
        title = str(candidate["title"]).strip()
        text = str(candidate["text"]).strip()
        return f"{title} {title} {text}".strip()

    def _load_json_metadata(self, key: str) -> object:
        raw = self.memory.get_index_metadata(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _tokenize(self, text: str) -> list[str]:
        tokens = [match.group(0).lower() for match in self.TOKEN_PATTERN.finditer(text)]
        expanded: list[str] = []
        for token in tokens:
            expanded.append(token)
            normalized = re.sub(r"[^a-z0-9]+", "", token)
            expanded.extend(self.QUERY_SYNONYMS.get(normalized, ()))
        return expanded

    def _cosine(self, left: list[float], right: np.ndarray) -> float:
        right_norm = np.linalg.norm(right)
        if right_norm == 0:
            return 0.0
        left_vector = np.array(left, dtype=np.float32)
        left_norm = np.linalg.norm(left_vector)
        if left_norm == 0:
            return 0.0
        return float(np.dot(left_vector, right) / (left_norm * right_norm))
