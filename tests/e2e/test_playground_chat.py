"""E2E — what the Playground sends to the model, turn after turn.

Hermetic: ``/v1/chat/completions`` is intercepted in the browser and answered
with a canned reply, so no provider is called. The tests read the request
bodies the page sent.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")

QUESTION = "Which model are you?"


def _completion(model: str) -> dict:
    return {
        "id": "chatcmpl-e2e",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": f"I am {model}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
    }


def _sse(model: str) -> str:
    chunk = {
        "id": "chatcmpl-e2e",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [
            {"index": 0, "delta": {"content": f"I am {model}"}, "finish_reason": None}
        ],
    }
    done = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(done)}\n\ndata: [DONE]\n\n"


async def _open_playground(dashboard, *, stream: bool) -> list:
    """Open the Playground with the Stream box set; return the model ids."""
    page = dashboard.page
    await dashboard.goto_tab("playground")
    await page.wait_for_function(
        "() => [...document.querySelectorAll('#pg-model option')]"
        ".filter(o => o.value).length >= 2",
        timeout=10_000,
    )
    if await page.is_checked("#pg-stream") != stream:
        await page.click("#pg-stream")
    return await page.eval_on_selector_all(
        "#pg-model option", "els => els.map(o => o.value).filter(Boolean)"
    )


async def _intercept(page, *, fail_first: bool = False) -> list:
    """Answer completions in the browser; return the log of request bodies."""
    sent: list = []

    async def answer(route):
        body = route.request.post_data_json
        sent.append(body)
        if fail_first and len(sent) == 1:
            await route.fulfill(status=400, json={"detail": "upstream refused"})
        elif body.get("stream"):
            await route.fulfill(
                status=200, content_type="text/event-stream", body=_sse(body["model"])
            )
        else:
            await route.fulfill(status=200, json=_completion(body["model"]))

    await page.route("**/v1/chat/completions", answer)
    return sent


async def _ask(page, model: str, text: str, answers: int) -> None:
    """Send `text` to `model` and wait for the `answers`-th assistant reply."""
    await page.select_option("#pg-model", model)
    await page.fill("#pg-input", text)
    await page.click("#btn-send-chat")
    await page.wait_for_function(
        """n => {
            const b = document.querySelectorAll('#pg-messages [data-role="assistant"]');
            return b.length === n && b[n - 1].textContent !== '...';
        }""",
        arg=answers,
        timeout=10_000,
    )


@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_switching_model_starts_a_new_context(dashboard, stream):
    page = dashboard.page
    first, second = (await _open_playground(dashboard, stream=stream))[:2]
    sent = await _intercept(page)

    await _ask(page, first, QUESTION, answers=1)
    await _ask(page, second, QUESTION, answers=2)

    # The second model must not receive the first model's answer: it would
    # take that identity over and repeat it.
    assert sent[1]["model"] == second
    assert sent[1]["messages"] == [{"role": "user", "content": QUESTION}]
    # The switch is visible, and each answer names the model it came from.
    assert await page.locator("#pg-messages [data-role='divider']").count() == 1
    labels = await page.eval_on_selector_all(
        "#pg-messages [data-role='assistant']", "els => els.map(e => e.dataset.model)"
    )
    assert labels == [first, second]

    # Staying on the same model keeps the conversation going.
    await _ask(page, second, "And now?", answers=3)
    assert [m["role"] for m in sent[2]["messages"]] == ["user", "assistant", "user"]
    assert sent[2]["messages"][1]["content"] == f"I am {second}"
    assert dashboard.js_errors == [], dashboard.js_errors


@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_failed_turn_is_not_sent_again(dashboard, stream):
    page = dashboard.page
    first = (await _open_playground(dashboard, stream=stream))[0]
    sent = await _intercept(page, fail_first=True)

    await _ask(page, first, "First try", answers=1)
    await _ask(page, first, QUESTION, answers=2)

    # The question that failed got no answer: it is not replayed to the model.
    assert sent[1]["messages"] == [{"role": "user", "content": QUESTION}]
