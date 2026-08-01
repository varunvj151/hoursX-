"""Semantic memory recall and knowledge chunking/retrieval."""

from hoursx.knowledge import split_text


async def test_memory_remember_and_recall_orders_by_relevance(services, seeded):
    async with services.db.session() as db:
        await services.memory.remember(
            db,
            workspace_id=seeded.workspace_id,
            text="The production database runs PostgreSQL sixteen",
        )
        await services.memory.remember(
            db, workspace_id=seeded.workspace_id, text="The team mascot is a red panda"
        )
    async with services.db.session() as db:
        hits = await services.memory.recall(
            db, workspace_id=seeded.workspace_id, query="which PostgreSQL database version"
        )
    assert hits and "PostgreSQL" in hits[0].text


async def test_memory_recall_is_workspace_scoped(services, seeded):
    async with services.db.session() as db:
        await services.memory.remember(
            db, workspace_id=seeded.workspace_id, text="workspace one secret fact"
        )
        hits = await services.memory.recall(
            db, workspace_id="other-workspace", query="workspace one secret fact"
        )
    assert hits == []


async def test_memory_importance_weights_ranking(services, seeded):
    async with services.db.session() as db:
        await services.memory.remember(
            db,
            workspace_id=seeded.workspace_id,
            text="deploy target region europe",
            importance=0.1,
        )
        await services.memory.remember(
            db,
            workspace_id=seeded.workspace_id,
            text="deploy target region europe west",
            importance=1.0,
        )
        hits = await services.memory.recall(
            db, workspace_id=seeded.workspace_id, query="deploy target region europe"
        )
    assert hits[0].score >= hits[-1].score


def test_split_text_respects_bounds_and_overlap():
    text = ("Sentence one is here. " * 40 + "\n\n") * 5
    chunks = split_text(text, chunk_size=400, overlap=80)
    assert len(chunks) > 3
    assert all(len(chunk) <= 400 for chunk in chunks)
    # Overlap: consecutive chunks share content.
    assert any(chunks[i][-40:] in chunks[i + 1] for i in range(len(chunks) - 1))


def test_split_text_empty_and_small():
    assert split_text("") == []
    assert split_text("tiny") == ["tiny"]


async def test_knowledge_ingest_and_search(services, seeded):
    from hoursx.db.models import Document

    async with services.db.session() as db:
        doc = Document(workspace_id=seeded.workspace_id, title="Runbook")
        db.add(doc)
        await db.flush()
        doc_id = doc.id
    async with services.db.session() as db:
        count = await services.knowledge.ingest(
            db,
            document_id=doc_id,
            text=(
                "To restart the ingestion worker, run the worker restart command. "
                "The scheduler fires every minute. "
                "Vector chunks are stored in the document_chunks table."
            ),
        )
    assert count >= 1
    async with services.db.session() as db:
        hits = await services.knowledge.search(
            session=db, workspace_id=seeded.workspace_id, query="restart the ingestion worker"
        )
    assert hits and hits[0].document_title == "Runbook"


async def test_knowledge_reingest_is_idempotent(services, seeded):
    from sqlalchemy import func, select

    from hoursx.db.models import Document, DocumentChunk

    async with services.db.session() as db:
        doc = Document(workspace_id=seeded.workspace_id, title="Doc")
        db.add(doc)
        await db.flush()
        doc_id = doc.id
    for _ in range(2):
        async with services.db.session() as db:
            await services.knowledge.ingest(db, document_id=doc_id, text="alpha beta gamma")
    async with services.db.session() as db:
        count = (
            await db.execute(
                select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == doc_id)
            )
        ).scalar_one()
    assert count == 1
