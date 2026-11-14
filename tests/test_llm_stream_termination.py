"""Gateway-opted-in SSE termination checks with a mocked transport."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

import hcca.llm as llm_module
from hcca.errors import ModelError
from hcca.llm import OpenAICompatLLM


USAGE = {
    "prompt_tokens": 11,
    "prompt_cache_hit_tokens": 7,
    "prompt_cache_miss_tokens": 4,
    "completion_tokens": 9,
    "completion_tokens_details": {"reasoning_tokens": 4},
}


def _sse(usage=USAGE):
    payload = {"choices": [{"delta": {"content": "partial", "reasoning_content": "abc"},
                            "finish_reason": "stop"}]}
    if usage is not None:
        payload["usage"] = usage
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def _complete(monkeypatch, body, *, marker="1", content_type="text/event-stream"):
    response = io.BytesIO(body)
    response.headers = {"Content-Type": content_type}
    if marker is not None:
        response.headers["X-HCCA-Require-Stream-Done"] = marker
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)
    client = OpenAICompatLLM(base_url="https://fixture.invalid/v1", api_key="fixture-key",
                             model="fixture", stream=True, json_mode=False)
    return client.complete([])


@pytest.mark.parametrize("content_type", ["text/event-stream", "Text/Event-Stream; charset=utf-8"])
@pytest.mark.parametrize("usage", [None, USAGE])
def test_required_done_rejects_sse_eof_and_keeps_partial_evidence(monkeypatch, content_type, usage):
    with pytest.raises(ModelError, match=r"ended before \[DONE\]") as caught:
        _complete(monkeypatch, _sse(usage), content_type=content_type)
    error = caught.value
    assert error.usage == error.meta["usage"] == (usage or {})
    assert error.meta["content_chars"] == 7
    assert error.meta["reasoning_chars"] == 3
    assert error.meta["finish_reason"] == "stop"
    assert error.meta["stream_diagnostics"]["termination"] == "error"
    assert error.meta["usage_complete"] is False
    assert error.meta["usage_missing"] is (usage is None)
    if usage is not None:
        assert error.meta["completion_tokens"] == 9
        assert error.meta["cache_hit_tokens"] == 7
        assert error.meta["cache_miss_tokens"] == 4
        assert error.meta["usage_partial"] is True


def test_required_done_preserves_successful_response_and_metadata(monkeypatch):
    monkeypatch.setattr(llm_module, "time", SimpleNamespace(time=lambda: 1000.0, monotonic=lambda: 1000.0))
    body = _sse() + b"data: [DONE]\n\n"
    legacy = _complete(monkeypatch, body, marker=None)
    strict = _complete(monkeypatch, body)
    assert strict == legacy
    assert strict.text == "partial"
    assert strict.meta["usage"] == USAGE
    assert strict.meta["stream_diagnostics"]["termination"] == "done"
    assert strict.meta["usage_complete"] is True


@pytest.mark.parametrize("marker", [None, "0", "true"])
def test_sse_eof_compatibility_without_the_opt_in_value(monkeypatch, marker):
    response = _complete(monkeypatch, _sse(), marker=marker)
    assert response.text == "partial"
    assert response.meta["usage"] == USAGE
    assert response.meta["stream_diagnostics"]["termination"] == "eof"
    assert response.meta["usage_complete"] is True


def test_required_done_header_keeps_json_response_compatibility(monkeypatch):
    payload = {"choices": [{"message": {"content": "FINAL"}, "finish_reason": "stop"}],
               "usage": USAGE}
    response = _complete(monkeypatch, json.dumps(payload).encode(), content_type="application/json")
    assert response.text == "FINAL"
    assert response.meta["usage"] == USAGE
    assert response.meta["stream_diagnostics"]["termination"] == "eof"
    assert response.meta["usage_complete"] is True
