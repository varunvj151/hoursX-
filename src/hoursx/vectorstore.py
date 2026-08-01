"""Vector storage behind a protocol.

The knowledge engine depends on :class:`VectorStore`, never on a concrete table
layout — so swapping the default row-scan implementation for pgvector (or an
external vector database) is a new class, not a rewrite of retrieval logic.

The default :class:`SqlVectorStore` keeps embeddings in ``document_chunks`` and
scores in Python. That is honest about its scale: exact, dependency-free, and
linear in the workspace's chunk count. :class:`PgVectorStore` documents the
migration path for deployments that outgrow it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hoursx.db.models import Document, DocumentChunk
from hoursx.providers.hashing import cosine_similarity


@dataclass(frozen=True)
class ScoredChunk:
    """One retrieval result, independent of how it was stored or scored."""

    document_id: str
    document_title: str
    text: str
    score: float
    chunk_index: int = 0


@dataclass(frozen=True)
class ChunkToStore:
    index: int
    text: str
    embedding: list[float]


@runtime_checkable
class VectorStore(Protocol):
    """Persistence + similarity search for document chunks."""

    async def replace_document_chunks(
        self,
        session: AsyncSession,
        *,
        document_id: str,
        workspace_id: str,
        chunks: list[ChunkToStore],
    ) -> int: ...

    async def similarity_search(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        embedding: list[float],
        query_terms: set[str],
        top_k: int,
        min_score: float,
    ) -> list[ScoredChunk]: ...

    async def delete_document(self, session: AsyncSession, *, document_id: str) -> None: ...


class SqlVectorStore:
    """Default store: embeddings as JSON rows, cosine scored in Python.

    Hybrid scoring adds a small keyword-overlap bonus on top of cosine
    similarity. Pure embeddings reliably miss exact identifiers — error codes,
    function names, ticket numbers — which are precisely what operators search
    for most often.
    """

    keyword_weight = 0.1

    async def replace_document_chunks(
        self,
        session: AsyncSession,
        *,
        document_id: str,
        workspace_id: str,
        chunks: list[ChunkToStore],
    ) -> int:
        # Delete-then-insert makes re-ingestion idempotent, so retrying a failed
        # ingestion can never leave duplicated chunks behind.
        await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
        for chunk in chunks:
            session.add(
                DocumentChunk(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    index=chunk.index,
                    text=chunk.text,
                    embedding=chunk.embedding,
                )
            )
        await session.flush()
        return len(chunks)

    async def similarity_search(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        embedding: list[float],
        query_terms: set[str],
        top_k: int,
        min_score: float,
    ) -> list[ScoredChunk]:
        rows = (
            await session.execute(
                select(DocumentChunk, Document.title)
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(DocumentChunk.workspace_id == workspace_id)
            )
        ).all()

        results: list[ScoredChunk] = []
        for chunk, title in rows:
            score = cosine_similarity(embedding, chunk.embedding)
            if query_terms:
                overlap = len(query_terms & set(chunk.text.lower().split()))
                score += self.keyword_weight * (overlap / len(query_terms))
            if score >= min_score:
                results.append(
                    ScoredChunk(
                        document_id=chunk.document_id,
                        document_title=title,
                        text=chunk.text,
                        score=round(score, 4),
                        chunk_index=chunk.index,
                    )
                )
        # Tie-break on (document_id, index) so equal scores order deterministically —
        # unstable result order would churn the model's prompt cache.
        results.sort(key=lambda r: (-r.score, r.document_id, r.chunk_index))
        return results[:top_k]

    async def delete_document(self, session: AsyncSession, *, document_id: str) -> None:
        await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))


class PgVectorStore(SqlVectorStore):
    """pgvector-backed store for large corpora.

    Inherits ingestion from the SQL store and overrides only search, pushing
    the nearest-neighbour scan into PostgreSQL. Enabling it requires the
    ``vector`` extension and an ivfflat/hnsw index on the embedding column;
    until that migration exists this subclass intentionally falls back to the
    parent implementation rather than silently returning wrong results.
    """

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled

    async def similarity_search(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        embedding: list[float],
        query_terms: set[str],
        top_k: int,
        min_score: float,
    ) -> list[ScoredChunk]:
        if not self.enabled:
            return await super().similarity_search(
                session,
                workspace_id=workspace_id,
                embedding=embedding,
                query_terms=query_terms,
                top_k=top_k,
                min_score=min_score,
            )
        raise NotImplementedError(
            "pgvector search requires the vector extension and an embedding index; "
            "run the pgvector migration before enabling this store"
        )
