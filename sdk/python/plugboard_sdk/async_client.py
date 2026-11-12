"""Optional httpx client for typed decisions and embeddings; no ML dependencies."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from .client import (
    USER_AGENT,
    PlugboardError,
    PlugboardTimeoutError,
    _error_from_response,
)


class PlugboardAsyncClient:
    """Async client for decisions and embeddings. Install
    ``plugboard-sdk[async]`` to use it.

    Errors carry the status, the gateway's ``code`` and ``retry_after``;
    decision errors never carry the answer's body, embedding errors do (as
    the sync client's).

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
                "User-Agent": USER_AGENT,
            },
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        what: str = "Decision",
        keep_body: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        import httpx

        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise PlugboardTimeoutError(f"{what} request timed out") from None
        except httpx.HTTPError:
            raise PlugboardError(
                0, f"{what} connection failed", code="connection_failed"
            ) from None
        if not response.is_success:
            # The body stays out of the message; its code and delay are kept.
            found = _error_from_response(
                response.status_code, response.text, response.headers.get("Retry-After")
            )
            raise PlugboardError(
                response.status_code,
                found.detail if keep_body else f"{what} request failed",
                code=found.code,
                retry_after=found.retry_after,
            )
        try:
            return response.json()
        except ValueError:
            raise PlugboardError(
                response.status_code, f"Invalid {what.lower()} response"
            ) from None

    async def embeddings(
        self,
        model: str,
        input: Union[str, List[str]],
        *,
        encoding_format: Optional[Literal["float", "base64"]] = None,
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Embed one text or a list of texts (``POST /v1/embeddings``); the raw
        response, as :meth:`PlugboardClient.embeddings`."""
        body: Dict[str, Any] = {"model": model, "input": input}
        if encoding_format is not None:
            body["encoding_format"] = encoding_format
        if user is not None:
            body["user"] = user
        return await self._request(
            "POST", "/v1/embeddings", what="Embedding", keep_body=True, json=body
        )

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
