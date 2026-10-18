"""Shared test constants."""

PROVIDER_A = {
    "id": "test-provider",
    "name": "Test Provider",
    "base_url": "http://fake-llm.test",
    "bearer_token": "secret-token-abc123",
    "default_model": "fake:model-v1",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}

PROVIDER_B = {
    "id": "other-provider",
    "name": "Other Provider",
    "base_url": "http://other-llm.test",
    "bearer_token": "other-token-xyz",
    "default_model": "other:model-v2",
    "models": [
        {"id": "other:model-v2", "name": "Model V2", "provider": "other"},
        {"id": "other:model-v3", "name": "Model V3", "provider": "other"},
    ],
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}

FAKE_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1774000000,
    "model": "fake:model-v1",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}

# SSE streaming fixtures
FAKE_STREAM_CHUNK_1 = b'data: {"id":"chatcmpl-s1","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hel"},"index":0}]}\n\n'
FAKE_STREAM_CHUNK_2 = b'data: {"id":"chatcmpl-s1","object":"chat.completion.chunk","choices":[{"delta":{"content":"lo!"},"index":0}]}\n\n'
FAKE_STREAM_DONE = b"data: [DONE]\n\n"
FAKE_STREAM_BODY = FAKE_STREAM_CHUNK_1 + FAKE_STREAM_CHUNK_2 + FAKE_STREAM_DONE

PROVIDER_OLLAMA = {
    "id": "ollama",
    "name": "Ollama",
    "base_url": "http://localhost:11434",
    "bearer_token": "",
    "default_model": "llama3",
    "is_active": True,
    "provider_type": "sovereign",
    "created_at": "2026-01-01T00:00:00+00:00",
}

FAKE_OLLAMA_TAGS = {
    "models": [
        {
            "name": "llama3:latest",
            "model": "llama3:latest",
            "size": 4661224676,
            "digest": "abc123",
            "modified_at": "2026-01-01T00:00:00Z",
        },
        {
            "name": "mistral:latest",
            "model": "mistral:latest",
            "size": 4109855744,
            "digest": "def456",
            "modified_at": "2026-01-02T00:00:00Z",
        },
    ]
}

FAKE_PRICING = {
    "month": "2026_03",
    "models": {
        "fake:model-v1": {"input_price": 3.0, "output_price": 15.0},
        "other:model-v2": {"input_price": 2.5, "output_price": 10.0},
    },
    "note": "Test pricing",
}
