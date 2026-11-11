"""Streamed completions keep the assistant reply and token counts in the audit log."""

import httpx
import pytest

from tests.test_messages_native import (
    CLAUDE_CODE_BODY,
    NATIVE_EVENTS,
    _sse_lines,
    _Stream,
    _use_anthropic_deployment,
)
from tests.test_stream_accounting_commit import configured_stream, request_body

pytestmark = pytest.mark.app


class _RecordingAuditService:
    def __init__(self, *, payload_max_bytes=None) -> None:
        self.records = []
        if payload_max_bytes is not None:
            self.ingestion_config = type("Config", (), {"payload_max_bytes": payload_max_bytes})()

    def record_event(self, event, *, payloads=None, critical=False):  # noqa: ANN001, ANN201
        del critical
        self.records.append((event, {payload.kind: payload.content_json for payload in payloads}))


async def _post_stream(test_app, endpoint):  # noqa: ANN001, ANN202
    async with configured_stream(test_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            return await client.post(
                endpoint, headers={"Authorization": "Bearer sk-test"}, json=request_body(endpoint)
            )


@pytest.mark.parametrize(
    "endpoint", ["/v1/chat/completions", "/v1/responses", "/v1/completions", "/v1/messages"]
)
async def test_stream_audit_stores_reply_and_tokens(test_app, endpoint) -> None:
    audit = _RecordingAuditService()
    test_app.state.audit_service = audit

    response = await _post_stream(test_app, endpoint)

    assert response.status_code == 200
    event, payloads = audit.records[-1]
    assert event.status == "success"
    assert event.input_tokens == 13
    assert event.output_tokens == 5
    reply = payloads["response"]
    assert reply["object"] == "chat.completion"
    assert reply["stream"] is True
    assert reply["id"] == "stream-commit"
    assert reply["choices"][0]["message"] == {"role": "assistant", "content": "answer"}
    assert reply["choices"][0]["finish_reason"] == "stop"
    assert reply["usage"]["total_tokens"] == 18
    assert "truncated" not in reply


async def test_stream_audit_reply_follows_the_audit_payload_limit(test_app) -> None:
    audit = _RecordingAuditService(payload_max_bytes=3)
    test_app.state.audit_service = audit

    response = await _post_stream(test_app, "/v1/chat/completions")

    assert "answer" in response.text
    _, payloads = audit.records[-1]
    assert payloads["response"]["choices"][0]["message"]["content"] is None
    assert payloads["response"]["truncated"] is True


async def test_stream_without_audit_service_still_streams(test_app) -> None:
    test_app.state.audit_service = None

    response = await _post_stream(test_app, "/v1/chat/completions")

    assert response.status_code == 200
    assert "data: [DONE]" in response.text


async def test_native_anthropic_stream_audit_keeps_thinking_and_tool_calls(
    client, test_app
) -> None:
    _use_anthropic_deployment(test_app)
    audit = _RecordingAuditService()
    test_app.state.audit_service = audit

    def stream(method, url, headers, json=None, content=None, timeout=None):  # noqa: ANN001, ANN202
        del method, url, headers, json, content, timeout
        return _Stream(_sse_lines(NATIVE_EVENTS), {})

    test_app.state.http_client.stream = stream
    response = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {test_app.state._test_key}"},
        json={**CLAUDE_CODE_BODY, "stream": True},
    )

    assert response.status_code == 200
    event, payloads = audit.records[-1]
    reply = payloads["response"]
    message = reply["choices"][0]["message"]
    assert reply["choices"][0]["finish_reason"] == "tool_calls"
    assert message["reasoning_content"] == "hmm"
    assert message["tool_calls"] == [
        {
            "id": "toolu_9",
            "type": "function",
            "function": {"name": "Bash", "arguments": '{"cmd":"ls"}'},
        }
    ]
    assert event.output_tokens == 30
    assert event.input_tokens == reply["usage"]["prompt_tokens"]
