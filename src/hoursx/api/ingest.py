"""Shared document-ingestion routine used by both task backends."""

from __future__ import annotations

from hoursx.db.models import Document
from hoursx.events import Event, EventType
from hoursx.observability import get_logger
from hoursx.services import AppServices

log = get_logger("api.ingest")


async def ingest_and_announce(services: AppServices, document_id: str, text: str) -> None:
    """Chunk + embed a document, record the outcome, and announce it. The
    document row always ends 'ready' or 'failed' — never silently 'pending'."""
    workspace_id = ""
    try:
        async with services.db.session() as db:
            chunk_count = await services.knowledge.ingest(db, document_id=document_id, text=text)
            document = await db.get(Document, document_id)
            assert document is not None
            workspace_id = document.workspace_id
    except Exception as exc:  # noqa: BLE001 — outcome must be recorded
        log.exception("ingestion of %s failed", document_id)
        async with services.db.session() as db:
            document = await db.get(Document, document_id)
            if document is not None:
                document.status = "failed"
                workspace_id = document.workspace_id
        chunk_count = 0
        payload = {"document_id": document_id, "ok": False, "error": str(exc)}
    else:
        payload = {"document_id": document_id, "ok": True, "chunks": chunk_count}
    if workspace_id:
        await services.bus.publish(
            Event(type=EventType.DOCUMENT_INGESTED, workspace_id=workspace_id, payload=payload)
        )
