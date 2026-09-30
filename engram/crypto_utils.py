"""Ed25519 signing for memory provenance."""
import base64
import json

import nacl.signing
import nacl.encoding
import nacl.exceptions

CANONICAL_FIELDS = ["id", "text", "subject", "value", "source_device", "ts"]


def canonical_payload(memory: dict) -> bytes:
    body = {k: memory[k] for k in CANONICAL_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def new_keypair() -> tuple[str, str]:
    """Returns (private_key_b64, public_key_b64)."""
    sk = nacl.signing.SigningKey.generate()
    vk = sk.verify_key
    return (
        base64.b64encode(bytes(sk)).decode(),
        base64.b64encode(bytes(vk)).decode(),
    )


def sign(memory: dict, private_key_b64: str) -> str:
    sk = nacl.signing.SigningKey(base64.b64decode(private_key_b64))
    sig = sk.sign(canonical_payload(memory)).signature
    return base64.b64encode(sig).decode()


def verify(memory: dict, signature_b64: str, public_key_b64: str) -> bool:
    try:
        vk = nacl.signing.VerifyKey(base64.b64decode(public_key_b64))
        vk.verify(canonical_payload(memory), base64.b64decode(signature_b64))
        return True
    except (nacl.exceptions.BadSignatureError, ValueError, TypeError):
        return False
