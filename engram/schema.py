"""Memory payload schema and Point construction."""
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

from qdrant_edge import Point, SparseVector

from . import crypto_utils
from .embeddings import embed_dense, embed_sparse_document

PRIVATE = "PRIVATE"
SYNC = "SYNC"

TRUST_VERIFIED = "verified"
TRUST_QUARANTINED = "quarantined"
TRUST_PENDING = "pending"

import re

PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN-like
    re.compile(r"\b\d{16}\b"),  # card number
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),  # email
    re.compile(r"\bpassword\b", re.IGNORECASE),
]


def looks_like_pii(text: str) -> bool:
    return any(p.search(text) for p in PII_PATTERNS)


@dataclass
class Memory:
    text: str
    subject: str
    value: str
    source_device: str
    sensitivity: str = SYNC
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = field(default_factory=time.time)
    trust_status: str = TRUST_PENDING
    corroborated_by: list[str] = field(default_factory=list)
    quarantine_reasons: list[str] = field(default_factory=list)
    score_breakdown: Optional[dict] = None
    signature: str = ""

    def __post_init__(self):
        if looks_like_pii(self.text) or looks_like_pii(self.value):
            self.sensitivity = PRIVATE

    def sign(self, private_key_b64: str):
        self.signature = crypto_utils.sign(asdict(self), private_key_b64)

    def verify(self, public_key_b64: str) -> bool:
        if not self.signature:
            return False
        return crypto_utils.verify(asdict(self), self.signature, public_key_b64)

    def payload(self) -> dict:
        return asdict(self)

    def to_point(self) -> Point:
        dense = embed_dense(self.text)
        sparse = embed_sparse_document(self.text)
        vector = {
            "dense": dense,
            "bm25": SparseVector(indices=sparse.indices, values=sparse.values),
        }
        return Point(id=self.id, vector=vector, payload=self.payload())

    @staticmethod
    def from_payload(payload: dict) -> "Memory":
        m = Memory(
            text=payload["text"],
            subject=payload["subject"],
            value=payload["value"],
            source_device=payload["source_device"],
            sensitivity=payload.get("sensitivity", SYNC),
        )
        m.id = payload["id"]
        m.ts = payload["ts"]
        m.trust_status = payload.get("trust_status", TRUST_PENDING)
        m.corroborated_by = payload.get("corroborated_by", [])
        m.quarantine_reasons = payload.get("quarantine_reasons", [])
        m.score_breakdown = payload.get("score_breakdown")
        m.signature = payload.get("signature", "")
        return m
