"""Ed25519 signing for memory provenance.

Timestamps are integer milliseconds: floats stored in a Qdrant payload were
observed to drift by 1 ULP on read-back, which silently breaks signatures
computed over them.
"""
import base64
import json

import nacl.exceptions
import nacl.signing

CANONICAL_FIELDS = ["id", "text", "subject", "value", "source_device", "ts"]


def canonical_payload(memory: dict) -> bytes:
    body = {k: memory[k] for k in CANONICAL_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def new_keypair() -> tuple[str, str]:
    sk = nacl.signing.SigningKey.generate()
    return base64.b64encode(bytes(sk)).decode(), base64.b64encode(bytes(sk.verify_key)).decode()


def sign(memory: dict, private_key_b64: str) -> str:
    sk = nacl.signing.SigningKey(base64.b64decode(private_key_b64))
    return base64.b64encode(sk.sign(canonical_payload(memory)).signature).decode()


def verify(memory: dict, signature_b64: str, public_key_b64: str) -> bool:
    if not signature_b64 or not public_key_b64:
        return False
    try:
        vk = nacl.signing.VerifyKey(base64.b64decode(public_key_b64))
        vk.verify(canonical_payload(memory), base64.b64decode(signature_b64))
        return True
    except (nacl.exceptions.BadSignatureError, ValueError, TypeError, KeyError):
        return False
