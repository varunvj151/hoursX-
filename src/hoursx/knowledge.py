"""Knowledge engine (RAG): chunking, ingestion, and retrieval.

The engine owns *policy* — how text is split, what gets embedded, what counts as
a match. Storage and similarity are delegated to a :class:`VectorStore`, so a
deployment can move to pgvector or an external vector database without touching
this file.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from hoursx.db.models import Document
from hoursx.errors import NotFoundError
from hoursx.providers.router import ModelRouter
from hoursx.vectorstore import ChunkToStore, ScoredChunk, SqlVectorStore, VectorStore

# Re-exported so callers keep one import site for retrieval results.
RetrievedChunk = ScoredChunk

_EMBED_BATCH = 64


def split_text(text: str, chunk_size: int = 1600, overlap: int = 200) -> list[str]:
    """Split into overlapping chunks, preferring paragraph/sentence boundaries.

    Windows advance by ``chunk_size - overlap``; each window is trimmed back to
    the last blank line or sentence end inside it when one exists past the
    midpoint, so chunks tend to end at natural boundaries.
    """
    if chunk_size <= overlap:
        raise ValueError("chunk_size must exceed overlap")
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        window = text[start : start + chunk_size]
        cut = len(window)
        if start + len(window) < len(text):  # not the final chunk — seek a boundary
            for boundary in ("\n\n", ". ", "\n"):
                pos = window.rfind(boundary)
                if pos > chunk_size // 2:
                    cut = pos + len(boundary)
                    break
        chunk = window[:cut].strip()
        if chunk:
            chunks.append(chunk)
        next_start = start + cut - overlap
        start = next_start if next_start > start else start + cut
    return chunks


class KnowledgeEngine:
    def __init__(
        self,
        router: ModelRouter,
        chunk_size: int = 1600,
        overlap: int = 200,
        store: VectorStore | None = None,
    ) -> None:
        self._router = router
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._store: VectorStore = store or SqlVectorStore()

    async def _embed_all(self, pieces: list[str]) -> list[list[float]]:
        """Embed in bounded batches — a large document must not become one
        oversized provider request that trips payload limits."""
        embeddings: list[list[float]] = []
        for start in range(0, len(pieces), _EMBED_BATCH):
            embeddings.extend(await self._router.embed(pieces[start : start + _EMBED_BATCH]))
        return embeddings

    async def ingest(self, session: AsyncSession, *, document_id: str, text: str) -> int:
        """(Re)build a document's chunks; returns the chunk count. Idempotent —
        prior chunks are replaced, so re-ingesting a failed document is safe."""
        document = await session.get(Document, document_id)
        if document is None:
            raise NotFoundError(f"document {document_id} not found", document_id=document_id)
        pieces = split_text(text, self._chunk_size, self._overlap)
        embeddings = await self._embed_all(pieces) if pieces else []
        stored = await self._store.replace_document_chunks(
            session,
            document_id=document_id,
            workspace_id=document.workspace_id,
            chunks=[
                ChunkToStore(index=index, text=piece, embedding=embedding)
                for index, (piece, embedding) in enumerate(zip(pieces, embeddings, strict=True))
            ],
        )
        document.chunk_count = stored
        document.status = "ready"
        await session.flush()
        return stored

    async def search(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        query: str,
        top_k: int = 6,
        min_score: float = 0.05,
    ) -> list[RetrievedChunk]:
        """Hybrid retrieval: vector similarity plus keyword-overlap bonus."""
        [query_vec] = await self._router.embed([query])
        terms = {term for term in query.lower().split() if len(term) > 2}
        return await self._store.similarity_search(
            session,
            workspace_id=workspace_id,
            embedding=query_vec,
            query_terms=terms,
            top_k=top_k,
            min_score=min_score,
        )

    async def forget_document(self, session: AsyncSession, *, document_id: str) -> None:
        """Drop a document's chunks (used when a document is deleted)."""
        await self._store.delete_document(session, document_id=document_id)
