"""Memory payload schema, PII router, and Point construction."""
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from qdrant_edge import Point

from . import crypto_utils
from .embeddings import document_vectors

PRIVATE = "PRIVATE"
SYNC = "SYNC"

TRUST_VERIFIED = "verified"
TRUST_SUPERSEDED = "superseded"

PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "card_number": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "phone": re.compile(r"\+?\b\d{1,3}[ -]?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}\b"),
    "credential": re.compile(r"\b(password|passwd|api[_ -]?key|secret)\b", re.IGNORECASE),
}

REQUIRED_FIELDS = {"id": str, "text": str, "subject": str, "value": str, "source_device": str, "ts": int}


def now_ms() -> int:
    return int(time.time() * 1000)


def detect_pii(*texts: str) -> list[str]:
    return sorted({name for name, p in PII_PATTERNS.items() for t in texts if p.search(t)})


class MalformedMemory(ValueError):
    pass


@dataclass
class Memory:
    text: str
    subject: str
    value: str
    source_device: str
    sensitivity: str = SYNC
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: int = field(default_factory=now_ms)
    trust_status: str = TRUST_VERIFIED
    corroborated_by: list[str] = field(default_factory=list)
    route_reason: str = ""
    score_breakdown: Optional[dict] = None
    signature: str = ""

    def apply_router(self):
        """PII anywhere in text/value forces PRIVATE, whatever the caller asked for."""
        pii = detect_pii(self.text, self.value)
        if pii:
            self.sensitivity = PRIVATE
            self.route_reason = "held: PII detected (" + ", ".join(pii) + ")"
        elif self.sensitivity == PRIVATE:
            self.route_reason = "held: marked PRIVATE by writer"
        else:
            self.route_reason = "queued: SYNC, no PII detected"

    def sign(self, private_key_b64: str):
        self.signature = crypto_utils.sign(asdict(self), private_key_b64)

    def verify(self, public_key_b64: str) -> bool:
        return crypto_utils.verify(asdict(self), self.signature, public_key_b64)

    def payload(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_payload(payload: dict) -> "Memory":
        if not isinstance(payload, dict):
            raise MalformedMemory("payload is not an object")
        for name, typ in REQUIRED_FIELDS.items():
            if not isinstance(payload.get(name), typ) or isinstance(payload.get(name), bool):
                raise MalformedMemory(f"field '{name}' missing or not {typ.__name__}")
        try:
            uuid.UUID(payload["id"])
        except ValueError:
            raise MalformedMemory("id is not a UUID")
        known = set(Memory.__dataclass_fields__)
        return Memory(**{k: v for k, v in payload.items() if k in known})


def to_points(memories: list[Memory]) -> list[Point]:
    vectors = document_vectors([m.text for m in memories])
    return [Point(id=m.id, vector=v, payload=m.payload()) for m, v in zip(memories, vectors)]
