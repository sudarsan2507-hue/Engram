"""EdgeShard setup: trusted + staging shards per device, with payload indexes
for the filters that enforce the trust boundary."""
import os
import shutil

from qdrant_edge import (
    EdgeConfig,
    EdgeShard,
    EdgeVectorParams,
    EdgeSparseVectorParams,
    Distance,
    UpdateOperation,
    UpdateMode,
    PayloadSchemaType,
    Point,
    Filter,
    FieldCondition,
    MatchValue,
    QueryRequest,
    Prefetch,
    Query,
    Fusion,
    SparseVector,
)

from .embeddings import DENSE_SIZE, embed_dense, embed_sparse_query

INDEXED_FIELDS = {
    "trust_status": PayloadSchemaType.Keyword,
    "sensitivity": PayloadSchemaType.Keyword,
    "subject": PayloadSchemaType.Keyword,
    "source_device": PayloadSchemaType.Keyword,
}


def _config() -> EdgeConfig:
    return EdgeConfig(
        vectors={"dense": EdgeVectorParams(size=DENSE_SIZE, distance=Distance.Cosine)},
        sparse_vectors={"bm25": EdgeSparseVectorParams()},
    )


def open_or_create(path: str) -> EdgeShard:
    if os.path.isdir(path) and os.listdir(path):
        return EdgeShard.load(path)
    os.makedirs(path, exist_ok=True)
    shard = EdgeShard.create(path, _config())
    for field_name, schema in INDEXED_FIELDS.items():
        shard.update(UpdateOperation.create_field_index(field_name, schema))
    shard.flush()
    return shard


def reset(path: str) -> EdgeShard:
    if os.path.isdir(path):
        shutil.rmtree(path)
    return open_or_create(path)


def upsert(shard: EdgeShard, points: list[Point]):
    if not points:
        return
    shard.update(UpdateOperation.upsert_points(points, update_mode=UpdateMode.Upsert))


def delete(shard: EdgeShard, point_ids: list[str]):
    if not point_ids:
        return
    shard.update(UpdateOperation.delete_points(point_ids))


def eq_filter(**kwargs) -> Filter:
    conds = [FieldCondition(key=k, match=MatchValue(value=v)) for k, v in kwargs.items()]
    return Filter(must=conds)


def hybrid_search(shard: EdgeShard, text: str, limit: int = 10, filter: Filter = None):
    dense = embed_dense(text)
    sparse = embed_sparse_query(text)
    req = QueryRequest(
        limit=limit,
        filter=filter,
        with_payload=True,
        prefetches=[
            Prefetch(limit=limit * 4, query=Query.Nearest(dense, using="dense"), filter=filter),
            Prefetch(
                limit=limit * 4,
                query=Query.Nearest(SparseVector(indices=sparse.indices, values=sparse.values), using="bm25"),
                filter=filter,
            ),
        ],
        query=Fusion.Rrf(k=60),
    )
    return shard.query(req)


def trusted_filter() -> Filter:
    return eq_filter(trust_status="verified")
