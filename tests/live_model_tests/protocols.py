"""Shared surface contracts for the live-test framework.

Extracted from ``d1_liveness`` per Council C5 arbitrage (resolves the
ARCH-DEBT opened in C4 D2-DEC-01). All dimensions import the surface
Protocol(s) from here so the contract has a single source of truth.

``SurfaceClient``           — the base contract every surface (API / MCP / UI)
                              satisfies: a ``chat()`` returning a unified dict.
``StreamingCapableSurface`` — a SurfaceClient whose ``chat()`` dict additionally
                              carries ``raw_sse_lines`` (wire-level SSE lines),
                              required by D5. Only the API HTTP surface
                              implements this in C5; MCP stdio and UI Playwright
                              return ``raw_sse_lines=None`` (or omit it), so D5
                              short-circuits to ``skipped:surface_non_streaming``.
                              Keeping this in a derived Protocol avoids forcing a
                              transport-specific field onto surfaces that cannot
                              produce it (LSP — Council C5 BLOCKER-3).
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Protocol, runtime_checkable

# Surface timeout multipliers — applied to catalog.timeout_s by every dimension.
SURFACE_TIMEOUT_MULTIPLIER: Dict[str, float] = {
    "api": 1.0,
    "mcp": 1.5,
    "ui": 2.0,
}


@runtime_checkable
class SurfaceClient(Protocol):
    """Base surface contract. Surfaces stay dimension-agnostic by returning a
    single unified dict from ``chat()``."""

    name: str  # "api" | "mcp" | "ui"

    def chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        stream: bool,
        timeout: float,
        max_tokens: int,
        temperature: float,
    ) -> Dict[str, Any]:
        """Return a unified dict::

            {
                "content": str,                  # final content (concatenated if stream)
                "reasoning": str,                # final reasoning (concatenated if stream)
                "finish_reason": str | None,
                "first_token_latency_ms": float | None,
                "done_received": bool,           # True if [DONE] received (stream only)
                "raw_chunks": list,              # parsed SSE chunks, for diagnostics
                "model_echoed": str | None,      # response.model field if present
                "request_id": str | None,        # X-Request-ID or equivalent
            }
        """
        ...


@runtime_checkable
class StreamingCapableSurface(SurfaceClient, Protocol):
    """A ``SurfaceClient`` whose ``chat()`` dict ALSO includes::

        "raw_sse_lines": List[str] | None   # wire-level "data: ..." lines, in
                                            # order, BEFORE json.loads — required
                                            # by D5 for structural inspection.

    Surfaces that cannot expose raw SSE frames (MCP stdio, UI DOM) either do not
    satisfy this Protocol or return ``raw_sse_lines=None``; D5 then skips them.
    """

    ...


@runtime_checkable
class BillingAwareSurface(SurfaceClient, Protocol):
    """A ``SurfaceClient`` that exposes billing data for D6 Cost accuracy.

    The ``supports_billing`` class attribute is a DISCRIMINANT (Council C6-bis
    BLOCKER-2): a bare ``Protocol`` body with only ``...`` is structurally
    identical to ``SurfaceClient`` at ``isinstance`` time, so D6 would never be
    able to skip a non-billing surface (MCP/UI). The flag must be ``True``.

    The ``chat()`` dict additionally carries::

        "billing": {
            "cost_usd": float | None,          # X-Cost-USD
            "prompt_tokens": int | None,       # X-Prompt-Tokens
            "completion_tokens": int | None,   # X-Completion-Tokens
            "provider": str | None,            # X-Provider
            "billed_model": str | None,        # response body billing.model (ROUTED model)
            "pricing_month": str | None,       # response body billing.pricing_month
            "cache_hit": bool,                 # X-Cache hit (skip — no fresh cost)
        } | None
    """

    supports_billing: ClassVar[bool]

    ...
