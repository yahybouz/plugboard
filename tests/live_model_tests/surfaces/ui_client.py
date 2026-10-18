"""UI (Playwright) surface client for the live-test framework.

Satisfies the base ``SurfaceClient`` Protocol (structural typing). Does NOT
satisfy ``StreamingCapableSurface`` (raw_sse_lines=None → D5 skip) nor
``BillingAwareSurface`` (supports_billing=False → D6 skip).

The UI surface drives the admin dashboard **Playground** (``static/index.html``)
exactly as a human would: pick a model, type the prompt, click *Send*, wait for
the turn to finish, then read the assistant bubble. The DOM is the contract,
exposed via the ``data-testid`` hooks added in P4-05:

    pg-model · pg-stream · pg-max-tokens · pg-input · pg-send · pg-status
    message-bubble[data-role=user|assistant]

Council C9 (2026-05-31), triplet:
  - PROPOSER : developer
  - CRITIC   : codex-coder
  - ARBITER  : developer-arbiter

The UI layer is intrinsically LOSSY. These decisions stop it from manufacturing
false PASSes (the whole point of the council):

  1. EMPTY-CONTENT FIDELITY [BLOCKER class]. The Playground rewrites an empty
     model response into a human sentinel ("(empty response)" or a truncation
     warning) before painting the bubble. If the client read the bubble text
     naively, D1 would see a non-empty string and falsely declare a dead/burned
     model "alive". The Playground therefore tags the assistant bubble with
     machine-readable attributes set BEFORE the cosmetic overwrite:
        data-content-empty="1"          → real content was empty
        data-finish-reason="<reason>"   → finish_reason when known
     The client trusts those attributes and maps empty content back to "".
     A defensive fallback also maps the two known sentinel STRINGS to ""/length
     in case a stale (cached) app.js without the attributes is served.

  2. TRANSPORT vs MODEL-CONTENT ERROR. Discrimination is done on the status
     line's ``data-state`` (sending|done|error), never on the bubble text: a
     model literally answering "Error: ..." with a SUCCESS turn is content, not
     a transport error (mirrors the MCP C8 M-2 decision).

  3. reasoning is NOT scraped from the Playground's <details> panel — the UI
     surface reports reasoning="". Documented limitation: D1's
     "alive_internal_only" verdict degrades to "empty_response" on the UI
     surface only (D3 already ignores reasoning; D2/D4 use content).

  4. finish_reason cannot be OBSERVED from a streamed DOM. It is taken from the
     ``data-finish-reason`` hook when present, else synthesized: "stop" on real
     content, "length"/None on the empty sentinels — never blindly guessed.

  5. FRESH CONTEXT per call. Every ``chat()`` runs in a brand-new browser
     context (cookies + history wiped) so no session or chat-history bleed
     leaks between (model, dimension) cases. The browser process itself is
     launched once and reused (launch is expensive; a context is cheap). The
     page's ``_pgHistory`` is implicitly reset to ``[]`` by the page reload in
     each fresh context (Council C9 MINOR-5).

  6. ALL generation params are driven (stream, max_tokens, temperature). Leaving
     ``pg-temp`` at its HTML default would silently override the caller's
     temperature and add non-determinism to D2/D4 (Council C9 MAJOR-1).

Testability mirrors the API (C7) and MCP (C8) surfaces: the browser work lives
behind an injectable ``UiDriver``. Unit tests pass a scripted ``FakeUiDriver``
so they never launch Chromium; only a real ``make live-tests --surface ui`` run
imports Playwright (the import is lazy, inside ``PlaywrightUiDriver``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Optional, Protocol, runtime_checkable

# Cosmetic sentinels written by app.js _pgSend() when the real content is empty.
# Kept here ONLY as a defensive fallback for a stale app.js lacking the
# data-content-empty hook (decision 1). Primary signal is the DOM attribute.
# COUPLED to app.js _pgSend() (the "(empty response)" / truncation strings) — if
# those strings (or their locale) change, update here too (Council C9 MINOR-3).
_EMPTY_SENTINEL = "(empty response)"
_TRUNCATION_PREFIX = "⚠️ Réponse tronquée avant le contenu"  # "⚠️ Réponse tronquée…"


# ── Exceptions ────────────────────────────────────────────────────────────────


class UiSurfaceError(Exception):
    """``error_type`` is consumed by dimension run_dX handlers (like
    ApiSurfaceError / McpSurfaceError)."""

    def __init__(self, error_type: str, detail: str = "") -> None:
        self.error_type = error_type
        super().__init__(f"[{error_type}] {detail}"[:300])


class UiModelUnavailable(UiSurfaceError):
    """The requested model is not offered by the Playground dropdown."""

    def __init__(self, model: str) -> None:
        super().__init__(
            "ui_model_unavailable", f"model {model!r} not in Playground dropdown"
        )


class UiTransportError(UiSurfaceError):
    """Browser/navigation failure (launch, goto, login, missing element)."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("ui_transport_error", detail)


# ── Driver contracts ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BubbleRead:
    """What the client needs to read off the final assistant bubble."""

    text: str
    content_empty: bool  # from data-content-empty="1"
    finish_reason: Optional[str]  # from data-finish-reason


@dataclass(frozen=True)
class TurnRead:
    """Result of one Playground turn (driver → client)."""

    status_state: str  # "done" | "error"
    status_text: str  # human status line (for error classification)
    bubble: BubbleRead


@runtime_checkable
class UiDriver(Protocol):
    """Browser primitive a ``UiSurfaceClient`` drives.

    ``run_turn`` performs ONE full Playground exchange in a fresh context and
    returns a :class:`TurnRead`. It MUST raise:
      - :class:`UiModelUnavailable` if ``model`` is absent from the dropdown,
      - ``TimeoutError`` if the turn does not reach a terminal state by the
        deadline (so the dimension attributes "timeout"),
      - :class:`UiTransportError` for any browser/navigation failure.
    """

    def run_turn(
        self,
        *,
        model: str,
        message: str,
        stream: bool,
        max_tokens: int,
        temperature: float,
        deadline: float,
    ) -> TurnRead: ...

    def close(self) -> None: ...


# ── Helpers (pure — unit-tested directly) ─────────────────────────────────────


def _extract_user_message(messages: List[Dict[str, Any]]) -> str:
    """The Playground has a single text box — use the last user message. A
    system message cannot be expressed in the UI (no field); it is dropped
    (documented limitation). No live dimension currently sends a system role."""
    user = next(
        (m for m in reversed(messages) if (m.get("role") or "").lower() == "user"), None
    )
    if user is None:
        raise ValueError("UiSurfaceClient.chat requires at least one user message")
    return user.get("content") or ""


def _match_option(model: str, option_values: List[str]) -> Optional[str]:
    """Resolve a bare catalog id to the dropdown's actual ``<option value>``.

    The Playground dropdown is populated from ``/v1/models`` which returns
    provider-PREFIXED ids (``Codex:gpt-5.4``, ``ollama:gpt-oss:120b-cloud``,
    ``claude-code:claude-opus-4-7``), while ``catalog.yaml`` and the API/MCP
    surfaces use the BARE id (``gpt-5.4``, ``gpt-oss:120b-cloud``). Without this
    resolution every catalog model would be a false ``ui_model_unavailable`` on
    the UI surface (found by the first real run, post-C9 council). Returns the
    option value to select, or None if genuinely absent. Prefix is split on the
    FIRST colon only, so ids that themselves contain colons (Ollama) survive.
    """
    if model in option_values:
        return model
    for v in option_values:
        prefix, sep, bare = v.partition(":")
        if sep and bare == model:
            return v
    return None


def _classify_error(status_text: str) -> str:
    """Map the Playground status line to a stable error_type. app.js sets either
    'Error <code>' (HTTP) or 'Connection error' (network/JS exception)."""
    s = (status_text or "").strip()
    low = s.lower()
    if low.startswith("error"):
        digits = "".join(ch for ch in s if ch.isdigit())
        if digits:
            return f"http_error:{digits}"
        return "http_error:unknown"
    if "connection error" in low:
        return "ui_connection_error"
    return "ui_error"


def _resolve_content(bubble: BubbleRead) -> tuple[str, Optional[str]]:
    """Return ``(content, finish_reason)`` from a successful turn's bubble,
    honouring the empty-content fidelity rule (decision 1)."""
    if bubble.content_empty:
        # Real content was empty; the visible text is a cosmetic sentinel.
        # Normalise finish_reason to None for parity with the MCP surface
        # (_make_unified_dict: "stop" if content else None). Keep only "length",
        # which carries the diagnostic "burned its token budget" signal so the
        # scorecard reports the same functional state identically across surfaces
        # (Council C9 MAJOR-2).
        fr = bubble.finish_reason if bubble.finish_reason == "length" else None
        return "", fr
    # Defensive fallback for a stale app.js without the data-content-empty hook.
    if bubble.text == _EMPTY_SENTINEL:
        return "", None
    if bubble.text.startswith(_TRUNCATION_PREFIX):
        return "", "length"
    return bubble.text, (bubble.finish_reason or "stop")


def _unified(content: str, finish_reason: Optional[str]) -> Dict[str, Any]:
    return {
        "content": content,
        "reasoning": "",  # decision 3 — UI does not scrape the panel
        "finish_reason": finish_reason,
        "first_token_latency_ms": None,  # DOM timing is unreliable; not measured
        "done_received": True,  # the turn reached a terminal "done" state
        "raw_chunks": [],
        "raw_sse_lines": None,  # not a streaming-wire surface → D5 skips
        "billing": None,  # supports_billing=False → D6 skips
        "model_echoed": None,  # response.model not exposed to the DOM
        "request_id": None,  # X-Request-ID not exposed to the DOM
    }


# ── UiSurfaceClient ───────────────────────────────────────────────────────────


class UiSurfaceClient:
    """Playground (browser) surface client. NOT thread-safe (one per worker)."""

    name: str = "ui"
    supports_billing: ClassVar[bool] = False

    def __init__(
        self,
        base_url: str = "http://localhost:8765",
        api_key: str = "",
        *,
        driver: Optional[UiDriver] = None,
        headless: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._headless = headless
        if driver is not None:
            self._driver = driver
            self._owned_driver = False
        else:
            # Lazy: build the real Playwright driver only when no fake is given.
            self._driver = PlaywrightUiDriver(self._base_url, headless=headless)
            self._owned_driver = True

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
        message = _extract_user_message(messages)
        deadline = time.monotonic() + timeout
        turn = self._driver.run_turn(
            model=model,
            message=message,
            stream=stream,
            max_tokens=max_tokens,
            temperature=temperature,
            deadline=deadline,
        )
        if turn.status_state == "error":
            raise UiSurfaceError(_classify_error(turn.status_text), turn.status_text)
        content, finish_reason = _resolve_content(turn.bubble)
        return _unified(content, finish_reason)

    def close(self) -> None:
        if self._owned_driver:
            try:
                self._driver.close()
            except Exception:  # noqa: BLE001
                pass
            self._owned_driver = (
                False  # idempotent: don't re-close via __del__ (C9 MINOR-4)
            )

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> "UiSurfaceClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ── PlaywrightUiDriver (real browser — Playwright imported lazily) ────────────

# Status terminal predicate: app.js sets pg-status data-state to
# "sending" → "done" | "error". We wait for done|error. JS is single-threaded
# and `data-state="done"` is the LAST write in _pgSend (after the bubble text and
# its data-* attributes), so reading the bubble right after this predicate fires
# is race-free — no MutationObserver needed (Council C9 MINOR-1).
_WAIT_DONE_JS = (
    "() => { const s = document.querySelector('[data-testid=\"pg-status\"]');"
    " return !!(s && (s.dataset.state === 'done' || s.dataset.state === 'error')); }"
)

# The dropdown ships a single disabled "Loading models..." option (value="").
# It is "populated" once at least one option carries a non-empty value. Waiting
# for this BEFORE select_option means a slow /v1/models load times out as a
# genuine timeout, not as a false "model unavailable" (Council C9 MINOR-2).
_MODELS_LOADED_JS = (
    "() => { const s = document.querySelector('[data-testid=\"pg-model\"]');"
    " return !!(s && Array.from(s.options).some(o => o.value)); }"
)


class PlaywrightUiDriver:
    """Concrete :class:`UiDriver` over Playwright's sync API.

    One Chromium process is launched lazily and reused; each ``run_turn`` opens a
    FRESH context (cookies/history wiped — decision 5) and closes it afterwards.
    Playwright is imported inside ``_ensure_browser`` so the framework venv never
    needs Playwright unless a real UI run is requested.
    """

    def __init__(self, base_url: str, *, headless: bool = True) -> None:
        self._base_url = base_url.rstrip("/")
        self._headless = headless
        self._pw = None
        self._browser = None

    def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright  # lazy import
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise UiTransportError(
                "playwright not installed in this venv; `pip install playwright "
                "&& playwright install chromium`"
            ) from exc
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self._headless)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            raise UiTransportError(f"failed to launch chromium: {exc}") from exc

    @staticmethod
    def _remaining_ms(deadline: float) -> float:
        ms = (deadline - time.monotonic()) * 1000.0
        if ms <= 0:
            raise TimeoutError("UI turn deadline exceeded before completion")
        return ms

    def run_turn(
        self,
        *,
        model: str,
        message: str,
        stream: bool,
        max_tokens: int,
        temperature: float,
        deadline: float,
    ) -> TurnRead:
        # Imported here too so the type is available without a module-level dep.
        from playwright.sync_api import (  # lazy import
            Error as PlaywrightError,
            TimeoutError as PWTimeoutError,
        )

        self._ensure_browser()
        assert self._browser is not None
        context = self._browser.new_context()
        page = context.new_page()
        try:
            page.goto(
                self._base_url,
                wait_until="domcontentloaded",
                timeout=self._remaining_ms(deadline),
            )
            # Auto-login on localhost makes #app visible (app.js removes .hidden).
            page.wait_for_selector(
                "#app:not(.hidden)", timeout=self._remaining_ms(deadline)
            )
            page.evaluate("location.hash = 'playground'")
            page.wait_for_selector(
                "#page-playground.active", timeout=self._remaining_ms(deadline)
            )

            # Wait for the dropdown to be POPULATED first, so a slow /v1/models
            # load times out as a real timeout rather than a false unavailable.
            page.wait_for_function(
                _MODELS_LOADED_JS, timeout=self._remaining_ms(deadline)
            )

            # The dropdown shows provider-PREFIXED ids ("Codex:gpt-5.4"); the
            # catalog/API use the bare id ("gpt-5.4"). Resolve before selecting —
            # else every catalog model is a false unavailable on the UI surface
            # (gap found by the first real run, fixed post-C9 council).
            option_values = page.eval_on_selector_all(
                '[data-testid="pg-model"] option',
                "els => els.map(o => o.value).filter(Boolean)",
            )
            resolved = _match_option(model, option_values)
            if resolved is None:
                raise UiModelUnavailable(model)
            try:
                page.select_option(
                    '[data-testid="pg-model"]',
                    value=resolved,
                    timeout=min(self._remaining_ms(deadline), 5000),
                )
            except PWTimeoutError as exc:
                raise UiModelUnavailable(model) from exc

            # Generation params: stream toggle, max tokens, AND temperature — all
            # three must be set. Leaving pg-temp at its HTML default (0.7) would
            # silently override the caller's temperature (D1 sends 0.0) and inject
            # noise into D2/D4 structured-JSON checks (Council C9 MAJOR-1).
            stream_cb = page.locator('[data-testid="pg-stream"]')
            if stream_cb.is_checked() != stream:
                stream_cb.click()
            page.fill('[data-testid="pg-max-tokens"]', str(max_tokens))
            page.fill('[data-testid="pg-temp"]', str(temperature))

            # Submit.
            page.fill('[data-testid="pg-input"]', message)
            page.click('[data-testid="pg-send"]')

            # Wait for the turn to terminate (done|error).
            try:
                page.wait_for_function(
                    _WAIT_DONE_JS, timeout=self._remaining_ms(deadline)
                )
            except PWTimeoutError as exc:
                raise TimeoutError(
                    f"UI turn did not finish within deadline: {exc}"
                ) from exc

            status_el = page.query_selector('[data-testid="pg-status"]')
            status_state = (
                (status_el.get_attribute("data-state") or "") if status_el else ""
            )
            status_text = (status_el.text_content() or "") if status_el else ""

            if status_state == "error":
                # The bubble is unread on the error path (chat() raises on it), so
                # use an empty BubbleRead — never pass status text as "content"
                # (Council C9 NIT-1).
                return TurnRead(
                    status_state="error",
                    status_text=status_text,
                    bubble=BubbleRead(text="", content_empty=False, finish_reason=None),
                )

            bubbles = page.query_selector_all(
                '[data-testid="message-bubble"][data-role="assistant"]'
            )
            if not bubbles:
                raise UiTransportError("no assistant bubble after a 'done' status")
            last = bubbles[-1]
            bubble = BubbleRead(
                text=last.text_content() or "",
                content_empty=(last.get_attribute("data-content-empty") == "1"),
                finish_reason=last.get_attribute("data-finish-reason") or None,
            )
            return TurnRead(status_state="done", status_text=status_text, bubble=bubble)
        except (UiSurfaceError, TimeoutError):
            raise
        except PlaywrightError as exc:
            raise UiTransportError(f"playwright error: {exc}") from exc
        finally:
            try:
                context.close()  # fresh-context discipline (decision 5)
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None
