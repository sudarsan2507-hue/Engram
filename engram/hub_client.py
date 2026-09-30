"""REST client a device uses to reach the hub. Accepts any httpx-compatible
client (e.g. FastAPI's TestClient) so tests can run hub + devices in-process."""
import httpx


class HubClient:
    def __init__(self, base_url: str = "", client=None):
        self.http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=15)

    def register(self, device_id: str, public_key: str) -> dict:
        r = self.http.post("/register", json={"device_id": device_id, "public_key": public_key})
        r.raise_for_status()
        return r.json()

    def registry(self) -> dict:
        r = self.http.get("/registry")
        r.raise_for_status()
        return r.json()

    def push(self, device_id: str, memories: list[dict]) -> dict:
        r = self.http.post("/push", json={"device_id": device_id, "memories": memories})
        r.raise_for_status()
        return r.json()

    def pull(self, since: int, exclude_device: str) -> dict:
        r = self.http.get("/pull", params={"since": since, "exclude_device": exclude_device})
        r.raise_for_status()
        return r.json()
