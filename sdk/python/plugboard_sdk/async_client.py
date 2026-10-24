"""Optional httpx client for typed decisions; no ML dependencies."""

from __future__ import annotations

from typing import Any, Dict, List, Literal

from .client import PlugboardError


class PlugboardAsyncClient:
    """Async decisions client. Install ``plugboard-sdk[async]`` to use it.

    Use ``async with`` or call :meth:`aclose` to close the HTTP connection pool.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://localhost:8765",
        timeout: float = 120.0,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        import httpx

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        import httpx

        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise PlugboardError(0, "Decision request timed out") from None
        except httpx.HTTPError:
            raise PlugboardError(0, "Decision connection failed") from None
        if not response.is_success:
            raise PlugboardError(response.status_code, "Decision request failed")
        try:
            return response.json()
        except ValueError:
            raise PlugboardError(
                response.status_code, "Invalid decision response"
            ) from None

    async def list_decision_models(self) -> List[Dict[str, Any]]:
        """List engines, availability, confidence semantics and capability limits."""
        return (await self._request("GET", "/v1/decisions/models")).get("data", [])

    async def decide(
        self,
        model: Literal["jev", "laya", "laya-multilingual"],
        state: str,
        questions: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Submit named typed questions to the dedicated decision route."""
        if model == "kev-0.8b":
            raise ValueError("Unsupported decision model")
        return await self._request(
            "POST",
            "/v1/decisions",
            json={"model": model, "state": state, "questions": questions},
        )
