"""EdgeShard setup and query helpers. The trust boundary is a payload filter
(trust_status=verified) backed by a keyword index, applied to every search."""
import os
import shutil
import time
from typing import Optional

from qdrant_edge import (
    CountRequest,
    Distance,
    EdgeConfig,
    EdgeShard,
    EdgeSparseVectorParams,
    EdgeVectorParams,
    FieldCondition,
    Filter,
    Fusion,
    IdfParams,
    MatchAny,
    MatchValue,
    Modifier,
    PayloadSchemaType,
    Point,
    Prefetch,
    Query,
    QueryRequest,
    RangeFloat,
    ScrollRequest,
    SearchParams,
    UpdateMode,
    UpdateOperation,
)

from .embeddings import DENSE_SIZE, embed_dense, embed_sparse_query

INDEXED_FIELDS = {
    "trust_status": PayloadSchemaType.Keyword,
    "sensitivity": PayloadSchemaType.Keyword,
    "subject": PayloadSchemaType.Keyword,
    "source_device": PayloadSchemaType.Keyword,
    "ts": PayloadSchemaType.Integer,
}

RRF_K = 60


def config(quantization=None) -> EdgeConfig:
    return EdgeConfig(
        vectors={"dense": EdgeVectorParams(size=DENSE_SIZE, distance=Distance.Cosine)},
        sparse_vectors={"bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)},
        quantization_config=quantization,
    )


def open_or_create(path: str, with_indexes: bool = True, quantization=None) -> EdgeShard:
    if os.path.isdir(path) and os.listdir(path):
        return EdgeShard.load(path)
    os.makedirs(path, exist_ok=True)
    shard = EdgeShard.create(path, config(quantization))
    if with_indexes:
        create_indexes(shard)
    return shard


def create_indexes(shard: EdgeShard):
    for field_name, schema in INDEXED_FIELDS.items():
        shard.update(UpdateOperation.create_field_index(field_name, schema))


def drop_indexes(shard: EdgeShard):
    for field_name in INDEXED_FIELDS:
        shard.update(UpdateOperation.delete_field_index(field_name))


def wipe(path: str):
    if os.path.isdir(path):
        shutil.rmtree(path)


def upsert(shard: EdgeShard, points: list[Point]):
    if points:
        shard.update(UpdateOperation.upsert_points(points, update_mode=UpdateMode.Upsert))


def delete(shard: EdgeShard, point_ids: list):
    if point_ids:
        shard.update(UpdateOperation.delete_points(point_ids))


def set_payload(shard: EdgeShard, point_ids: list, payload: dict):
    if point_ids:
        shard.update(UpdateOperation.set_payload(point_ids, payload))


def match(key: str, value) -> FieldCondition:
    if isinstance(value, (list, tuple, set)):
        return FieldCondition(key=key, match=MatchAny(any=list(value)))
    return FieldCondition(key=key, match=MatchValue(value=value))


def eq_filter(**kwargs) -> Filter:
    return Filter(must=[match(k, v) for k, v in kwargs.items()])


def trusted_filter(extra: Optional[list] = None) -> Filter:
    return Filter(must=[match("trust_status", "verified"), *(extra or [])])


def ts_after(ms: int) -> FieldCondition:
    return FieldCondition(key="ts", range=RangeFloat(gt=ms))


def scroll_all(shard: EdgeShard, filter: Optional[Filter] = None, with_vector=False) -> list:
    out, offset = [], None
    while True:
        records, offset = shard.scroll(
            ScrollRequest(offset=offset, limit=512, filter=filter, with_payload=True, with_vector=with_vector)
        )
        out.extend(records)
        if offset is None:
            return out


def count(shard: EdgeShard, filter: Optional[Filter] = None) -> int:
    return shard.count(CountRequest(exact=True, filter=filter))


def query_vectors(text: str) -> tuple[list[float], object]:
    return embed_dense(text), embed_sparse_query(text)


def hybrid_query(shard: EdgeShard, dense, sparse, limit: int = 10, filter: Optional[Filter] = None,
                 params: Optional[SearchParams] = None, scoped_idf: bool = True):
    """Dense + BM25 prefetches, each pre-filtered, fused with RRF.

    With scoped_idf, BM25's IDF statistics are computed over the filtered (trusted) corpus only, so
    superseded or unverified documents cannot shift term weights for trusted results."""
    sparse_params = SearchParams(idf=IdfParams(corpus=filter)) if (filter is not None and scoped_idf) else params
    req = QueryRequest(
        limit=limit,
        filter=filter,
        with_payload=True,
        prefetches=[
            Prefetch(limit=limit * 4, query=Query.Nearest(dense, using="dense"), filter=filter, params=params),
            Prefetch(limit=limit * 4, query=Query.Nearest(sparse, using="bm25"), filter=filter, params=sparse_params),
        ],
        query=Fusion.Rrf(k=RRF_K),
    )
    return shard.query(req)


def hybrid_search(shard: EdgeShard, text: str, limit: int = 10, filter: Optional[Filter] = None):
    """Returns (hits, timings) with query embedding and vector search timed separately."""
    t0 = time.perf_counter()
    dense, sparse = query_vectors(text)
    t1 = time.perf_counter()
    hits = hybrid_query(shard, dense, sparse, limit=limit, filter=filter)
    t2 = time.perf_counter()
    return hits, {"embed_ms": round((t1 - t0) * 1000, 2), "search_ms": round((t2 - t1) * 1000, 2)}


def dense_neighbors(shard: EdgeShard, dense, limit: int, filter: Filter, score_threshold: float):
    return shard.query(
        QueryRequest(
            limit=limit,
            query=Query.Nearest(dense, using="dense"),
            filter=filter,
            with_payload=True,
            score_threshold=score_threshold,
        )
    )
