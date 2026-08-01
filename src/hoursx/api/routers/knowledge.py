"""Knowledge base: document upload and retrieval search."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from hoursx.api.deps import Actor, get_services, require
from hoursx.api.ingest import ingest_and_announce
from hoursx.api.schemas import DocumentIn, DocumentOut, SearchHit
from hoursx.auth import Permission
from hoursx.db.models import Document
from hoursx.services import AppServices

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


def _out(document: Document) -> DocumentOut:
    return DocumentOut(
        id=document.id,
        title=document.title,
        source=document.source,
        status=document.status,
        chunk_count=document.chunk_count,
        created_at=document.created_at,
    )


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    actor: Actor = Depends(require(Permission.KNOWLEDGE_READ)),
    services: AppServices = Depends(get_services),
) -> list[DocumentOut]:
    async with services.db.session() as db:
        rows = (
            (
                await db.execute(
                    select(Document)
                    .where(Document.workspace_id == actor.workspace.id)
                    .order_by(Document.created_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        return [_out(row) for row in rows]


@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    body: DocumentIn,
    actor: Actor = Depends(require(Permission.KNOWLEDGE_WRITE)),
    services: AppServices = Depends(get_services),
) -> DocumentOut:
    """Create the document and ingest it asynchronously; watch the
    ``document.ingested`` event or poll the document status."""
    async with services.db.session() as db:
        document = Document(workspace_id=actor.workspace.id, title=body.title, source=body.source)
        db.add(document)
        await db.flush()
        document_id = document.id
        out = _out(document)

    if services.settings.task_backend == "arq":
        from hoursx.jobs import enqueue_job

        await enqueue_job(services.settings, "ingest_document_job", document_id, body.text)
    else:
        asyncio.get_running_loop().create_task(
            ingest_and_announce(services, document_id, body.text)
        )
    return out


@router.get("/search", response_model=list[SearchHit])
async def search(
    q: str = Query(min_length=2),
    actor: Actor = Depends(require(Permission.KNOWLEDGE_READ)),
    services: AppServices = Depends(get_services),
) -> list[SearchHit]:
    async with services.db.session() as db:
        hits = await services.knowledge.search(
            db,
            workspace_id=actor.workspace.id,
            query=q,
            top_k=services.settings.retrieval_top_k,
        )
        return [
            SearchHit(
                document_id=hit.document_id,
                document_title=hit.document_title,
                text=hit.text,
                score=hit.score,
            )
            for hit in hits
        ]
