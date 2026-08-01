"""Vector store protocol conformance and the audit trail."""

from dataclasses import FrozenInstanceError

import pytest

from hoursx.audit import AuditAction, recent_events, record
from hoursx.db.models import Document
from hoursx.vectorstore import (
    ChunkToStore,
    PgVectorStore,
    ScoredChunk,
    SqlVectorStore,
    VectorStore,
)

# ------------------------------------------------------------- vector store


def test_sql_store_satisfies_the_protocol():
    assert isinstance(SqlVectorStore(), VectorStore)


def test_pgvector_store_also_satisfies_the_protocol():
    # The documented swap path must typecheck, not just exist in prose.
    assert isinstance(PgVectorStore(), VectorStore)


async def _document(services, seeded, title: str = "Doc") -> str:
    async with services.db.session() as db:
        doc = Document(workspace_id=seeded.workspace_id, title=title)
        db.add(doc)
        await db.flush()
        return doc.id


async def test_store_replaces_rather_than_appends(services, seeded):
    store = SqlVectorStore()
    doc_id = await _document(services, seeded)
    for _ in range(3):
        async with services.db.session() as db:
            count = await store.replace_document_chunks(
                db,
                document_id=doc_id,
                workspace_id=seeded.workspace_id,
                chunks=[ChunkToStore(index=0, text="alpha", embedding=[1.0, 0.0])],
            )
    assert count == 1
    async with services.db.session() as db:
        hits = await store.similarity_search(
            db,
            workspace_id=seeded.workspace_id,
            embedding=[1.0, 0.0],
            query_terms=set(),
            top_k=10,
            min_score=0.0,
        )
    assert len(hits) == 1


async def test_similarity_ranks_closer_vectors_first(services, seeded):
    store = SqlVectorStore()
    doc_id = await _document(services, seeded)
    async with services.db.session() as db:
        await store.replace_document_chunks(
            db,
            document_id=doc_id,
            workspace_id=seeded.workspace_id,
            chunks=[
                ChunkToStore(index=0, text="near", embedding=[1.0, 0.0]),
                ChunkToStore(index=1, text="far", embedding=[0.0, 1.0]),
            ],
        )
    async with services.db.session() as db:
        hits = await store.similarity_search(
            db,
            workspace_id=seeded.workspace_id,
            embedding=[1.0, 0.05],
            query_terms=set(),
            top_k=5,
            min_score=-1.0,
        )
    assert [hit.text for hit in hits][0] == "near"


async def test_keyword_overlap_boosts_exact_identifiers(services, seeded):
    """Pure embeddings miss literal tokens like error codes; the bonus is why."""
    store = SqlVectorStore()
    doc_id = await _document(services, seeded)
    async with services.db.session() as db:
        await store.replace_document_chunks(
            db,
            document_id=doc_id,
            workspace_id=seeded.workspace_id,
            chunks=[
                ChunkToStore(index=0, text="ERR_QUOTA_EXCEEDED handling", embedding=[1.0, 0.0]),
                ChunkToStore(index=1, text="unrelated prose", embedding=[1.0, 0.0]),
            ],
        )
    async with services.db.session() as db:
        hits = await store.similarity_search(
            db,
            workspace_id=seeded.workspace_id,
            embedding=[1.0, 0.0],
            query_terms={"err_quota_exceeded"},
            top_k=5,
            min_score=0.0,
        )
    assert hits[0].text.startswith("ERR_QUOTA_EXCEEDED")


async def test_min_score_filters_weak_matches(services, seeded):
    store = SqlVectorStore()
    doc_id = await _document(services, seeded)
    async with services.db.session() as db:
        await store.replace_document_chunks(
            db,
            document_id=doc_id,
            workspace_id=seeded.workspace_id,
            chunks=[ChunkToStore(index=0, text="orthogonal", embedding=[0.0, 1.0])],
        )
    async with services.db.session() as db:
        hits = await store.similarity_search(
            db,
            workspace_id=seeded.workspace_id,
            embedding=[1.0, 0.0],
            query_terms=set(),
            top_k=5,
            min_score=0.5,
        )
    assert hits == []


async def test_search_is_workspace_isolated(services, seeded):
    store = SqlVectorStore()
    doc_id = await _document(services, seeded)
    async with services.db.session() as db:
        await store.replace_document_chunks(
            db,
            document_id=doc_id,
            workspace_id=seeded.workspace_id,
            chunks=[ChunkToStore(index=0, text="tenant secret", embedding=[1.0, 0.0])],
        )
    async with services.db.session() as db:
        hits = await store.similarity_search(
            db,
            workspace_id="intruder-workspace",
            embedding=[1.0, 0.0],
            query_terms=set(),
            top_k=5,
            min_score=-1.0,
        )
    assert hits == []


async def test_result_ordering_is_deterministic_on_ties(services, seeded):
    """Unstable ordering across identical scores would churn the prompt cache."""
    store = SqlVectorStore()
    doc_id = await _document(services, seeded)
    async with services.db.session() as db:
        await store.replace_document_chunks(
            db,
            document_id=doc_id,
            workspace_id=seeded.workspace_id,
            chunks=[
                ChunkToStore(index=i, text=f"chunk {i}", embedding=[1.0, 0.0]) for i in range(5)
            ],
        )
    orders = []
    for _ in range(3):
        async with services.db.session() as db:
            hits = await store.similarity_search(
                db,
                workspace_id=seeded.workspace_id,
                embedding=[1.0, 0.0],
                query_terms=set(),
                top_k=5,
                min_score=-1.0,
            )
        orders.append([hit.chunk_index for hit in hits])
    assert orders[0] == orders[1] == orders[2]


async def test_delete_document_removes_its_chunks(services, seeded):
    store = SqlVectorStore()
    doc_id = await _document(services, seeded)
    async with services.db.session() as db:
        await store.replace_document_chunks(
            db,
            document_id=doc_id,
            workspace_id=seeded.workspace_id,
            chunks=[ChunkToStore(index=0, text="gone soon", embedding=[1.0, 0.0])],
        )
    async with services.db.session() as db:
        await store.delete_document(db, document_id=doc_id)
    async with services.db.session() as db:
        hits = await store.similarity_search(
            db,
            workspace_id=seeded.workspace_id,
            embedding=[1.0, 0.0],
            query_terms=set(),
            top_k=5,
            min_score=-1.0,
        )
    assert hits == []


async def test_pgvector_falls_back_until_migration_enabled(services, seeded):
    """Disabled pgvector must degrade to correct results, never wrong ones."""
    store = PgVectorStore(enabled=False)
    doc_id = await _document(services, seeded)
    async with services.db.session() as db:
        await store.replace_document_chunks(
            db,
            document_id=doc_id,
            workspace_id=seeded.workspace_id,
            chunks=[ChunkToStore(index=0, text="fallback works", embedding=[1.0, 0.0])],
        )
    async with services.db.session() as db:
        hits = await store.similarity_search(
            db,
            workspace_id=seeded.workspace_id,
            embedding=[1.0, 0.0],
            query_terms=set(),
            top_k=5,
            min_score=-1.0,
        )
    assert [hit.text for hit in hits] == ["fallback works"]


async def test_pgvector_refuses_to_guess_when_enabled_without_migration(services, seeded):
    store = PgVectorStore(enabled=True)
    async with services.db.session() as db:
        with pytest.raises(NotImplementedError):
            await store.similarity_search(
                db,
                workspace_id=seeded.workspace_id,
                embedding=[1.0],
                query_terms=set(),
                top_k=1,
                min_score=0.0,
            )


def test_scored_chunk_is_immutable():
    chunk = ScoredChunk(document_id="d", document_title="t", text="x", score=1.0)
    with pytest.raises(FrozenInstanceError):
        chunk.score = 2.0  # type: ignore[misc]


# --------------------------------------------------------------------- audit


async def test_audit_records_and_reads_back(services, seeded):
    async with services.db.session() as db:
        await record(
            db,
            workspace_id=seeded.workspace_id,
            actor_user_id=seeded.user_id,
            action=AuditAction.MEMBER_ADDED,
            target_type="user",
            target_id="u-123",
            role="admin",
        )
    async with services.db.session() as db:
        events = await recent_events(db, workspace_id=seeded.workspace_id)
    assert len(events) == 1
    assert events[0].action == "member.added"
    assert events[0].detail["role"] == "admin"


async def test_audit_redacts_secret_detail(services, seeded):
    async with services.db.session() as db:
        await record(
            db,
            workspace_id=seeded.workspace_id,
            actor_user_id=seeded.user_id,
            action=AuditAction.API_KEY_CREATED,
            target_type="api_key",
            target_id="k-1",
            api_key="hx_supersecret",
        )
    async with services.db.session() as db:
        events = await recent_events(db, workspace_id=seeded.workspace_id)
    assert events[0].detail["api_key"] == "***"


async def test_audit_is_workspace_scoped(services, seeded):
    async with services.db.session() as db:
        await record(
            db,
            workspace_id=seeded.workspace_id,
            actor_user_id=seeded.user_id,
            action=AuditAction.AGENT_CREATED,
            target_type="agent_profile",
            target_id="a-1",
        )
    async with services.db.session() as db:
        events = await recent_events(db, workspace_id="other-workspace")
    assert events == []


async def test_audit_returns_newest_first(services, seeded):
    async with services.db.session() as db:
        for index in range(3):
            await record(
                db,
                workspace_id=seeded.workspace_id,
                actor_user_id=seeded.user_id,
                action=AuditAction.AGENT_UPDATED,
                target_type="agent_profile",
                target_id=f"a-{index}",
            )
    async with services.db.session() as db:
        events = await recent_events(db, workspace_id=seeded.workspace_id)
    assert [e.target_id for e in events][0] == "a-2"


async def test_audit_failure_never_breaks_the_audited_action(services, seeded):
    """A broken audit write must not roll back the action it describes."""

    class _Exploding:
        def add(self, _obj):
            raise RuntimeError("audit table unavailable")

        async def flush(self):
            raise RuntimeError("audit table unavailable")

    await record(
        _Exploding(),  # type: ignore[arg-type]
        workspace_id=seeded.workspace_id,
        actor_user_id=seeded.user_id,
        action=AuditAction.RUN_CANCELLED,
        target_type="run",
        target_id="r-1",
    )  # must not raise
