"""Thin REST client a Device uses to talk to the hub."""
import httpx


class HubClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def register(self, device_id: str, public_key: str, trust_score: float = 0.8):
        httpx.post(
            f"{self.base_url}/register",
            json={"device_id": device_id, "public_key": public_key, "trust_score": trust_score},
            timeout=5,
        ).raise_for_status()

    def registry(self) -> dict:
        r = httpx.get(f"{self.base_url}/registry", timeout=5)
        r.raise_for_status()
        return r.json()

    def push(self, device_id: str, public_key: str, memories: list[dict]):
        r = httpx.post(
            f"{self.base_url}/push",
            json={"device_id": device_id, "public_key": public_key, "memories": memories},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def pull(self, exclude_device: str) -> list[dict]:
        r = httpx.get(f"{self.base_url}/pull", params={"exclude_device": exclude_device}, timeout=10)
        r.raise_for_status()
        return r.json()
