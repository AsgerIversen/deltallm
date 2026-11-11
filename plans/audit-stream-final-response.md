# Audit the final response of streamed text requests

Status: implemented in the working tree, not committed. No pull request or upstream issue exists yet.
Branch: `auditLogsFinalResponse`. Base: `f65c2a3e`.

## Problem

When an organization has `audit_content_storage_enabled = true`, DeltaLLM stores the request and
response payloads of data-plane text requests in the audit log.

For a streamed request (`stream: true`), the stored response payload is only a placeholder:

```json
{"object": "message"}
```

The value is `chat.completion.chunk`, `text_completion`, `response` or `message`, by endpoint. The
source is `emit_stream_success` in `src/chat/telemetry.py`:

```python
response_data={"object": stream_response_object},
```

The same call also does not set `input_tokens` and `output_tokens`. These columns are `null` for all
streamed events, but the gateway has the usage (`usage_data`) at that point.

### Effect

Coding agents (Claude Code, Codex, opencode, Cursor) and most chat UIs always stream. For them:

- The audit log does not contain the last assistant reply of a conversation. Each request sends the
  earlier turns again, so earlier replies are in later request payloads. The last reply is only in
  the stream that went to the client.
- Tool calls that the model made in the last turn are lost.
- Token columns are empty, so audit-based reports show 0 tokens for streamed traffic.

A downstream consumer (an internal transcript viewer that reads `/ui/api/audit/events`) found this
problem. On a production gateway, all 2252 text events were streamed `/v1/messages` calls from
Claude Code through an Anthropic deployment on Azure. All of them had `input_tokens = null`, and all
stored responses were `{"object": "message"}`.

### What other gateways do

- LiteLLM rebuilds a streamed response from its chunks (`litellm.stream_chunk_builder`). The
  logging callback gets the full response after the stream ends. Content storage in spend logs is
  opt-in (`store_prompts_in_spend_logs`).
- Helicone parses streamed responses and estimates token counts when the provider sends none.
- Cloudflare AI Gateway logs the prompt and response. The `cf-aig-collect-log-payload` header
  disables payload storage for each request.

DeltaLLM already rebuilds streams in two places: the stream cache (`src/cache/streaming.py`) and the
MCP tool loop (`src/chat/mcp_stream_buffer.py`). The audit is the only consumer that does not.

## Change

### 1. One shared reply accumulator: `src/chat/stream_reply.py`

`StreamReplyAccumulator` folds canonical `chat.completion.chunk` deltas into one assistant message:
text, `reasoning_content`, `reasoning_details`, `refusal` and tool calls. It is the logic from
`MCPStreamHopBuffer`, moved out. The MCP message does not include `refusal`, as before. RULES.md section 3 says to reuse an abstraction
when it has the same semantics. It also says not to add a second, similar implementation.

- `MCPStreamHopBuffer` now delegates to the accumulator. It has no size limit, because the next MCP
  hop must carry the exact assistant turn. Its behavior does not change. All `tests/mcp` tests pass.
- The audit uses the accumulator with `max_chars`. The limit applies to all retained strings: text,
  reasoning, refusal, tool-call IDs, types, names and arguments, and the JSON size of reasoning
  details. When the limit is reached, the accumulator drops later data and sets `truncated`. In
  bounded mode it also keeps a maximum of 128 tool calls.
- A reasoning detail with an array or object `index` is treated as a detail without an index, so
  malformed provider data cannot stop the stream.
- `completion(fallback_model=..., usage=...)` returns a `chat.completion` body. The body also has
  `stream: true`, and it has `truncated: true` only when the accumulator dropped text.

### 2. Reuse the existing parse: `src/chat/stream_usage.py`

`StreamUsageTracker` already decodes each stream line with `json.loads`. It now takes an optional
`reply` accumulator and gives it the decoded chunk. Thus a line is not decoded two times.

### 3. Create the accumulator only when an audit follows: `src/chat/audit.py`

`stream_reply_accumulator(request)` returns `None` in these conditions:

- `app.state.audit_service` is `None`.
- The request has an accounting v2 operation handle (`AccountingOperationHandle`).
  `emit_text_audit_event` returns early in that case, so no text audit event is written.

Otherwise it returns an accumulator. The limit is `audit_service.ingestion_config.payload_max_bytes`
(default 1 MiB). The existing `_audited_by_accounting` helper owns the accounting check, so the two
functions use the same condition.

### 4. Write the reply and tokens: `src/chat/telemetry.py` and `src/routers/chat.py`

- `handle_chat_like_request` creates the tracker with the accumulator. It gives
  `reply=stream_usage.reply.completion(...)` to `emit_stream_success`.
- `emit_stream_success` writes `reply or {"object": stream_response_object}` as `response_data`. It
  writes `input_tokens` and `output_tokens` from the usage that it already resolved for billing.

The callback payload and the guardrail post-call hook still get the placeholder. This change does
not modify that contract (see open questions).

### 5. Documentation

`docs/features/audit-log.md` has a new section, "Streamed responses".

## Why the canonical stream works for all endpoints

All four text endpoints use `handle_chat_like_request`. The canonical line that `StreamUsageTracker`
reads is always an OpenAI `chat.completion.chunk`:

- `/v1/chat/completions`: the provider stream, or a translation to chat chunks.
- `/v1/completions`, `/v1/responses` and `/v1/messages` with translation: the edge translates the
  canonical chunks to the client format. The tracker reads them before the translation.
- Native `/v1/messages` and native `/v1/responses`: `NativeSSEFrame` is a `str` that holds the
  canonical chunk. The provider event is in `.native`. `_NativeStreamRelay` in
  `src/providers/anthropic_messages.py` makes text, thinking and `tool_use` deltas.

Thus the stored body always has the `chat.completion` format, for all endpoints. This is also the
format of non-streamed `/v1/chat/completions` audit payloads. The non-streamed payloads of the other
endpoints use the client format (Anthropic `message`, Responses `response`). See open question 1.

## Invariants and budgets (RULES.md sections 3, 7 and 10)

- Source of truth: the canonical chunks that the gateway already reads for usage.
- Owner: `emit_stream_success` stays the one owner of the success audit event for a stream.
- Tenant scope: there is no change. The content policy (`audit_content_storage_enabled`) is applied
  at ingestion, as before. When content storage is off, the reply is redacted like other payloads.
- Latency: there is no new SQL, Redis or network call. For each line, the gateway does one extra
  dictionary walk on an object that is already decoded. At the end of the stream, it does one
  `"".join`. Time to first token does not change.
- Memory: each audited stream holds a maximum of `audit_payload_max_bytes` characters of reply
  strings, plus the bounded overhead of Python containers (128 tool calls, 128 reasoning details). The
  request body (`request_data`) is already in memory for the same audit event, so the maximum memory
  per stream is about two times the earlier value.
- Failure mode: the accumulator does not raise on malformed chunks. `StreamUsageTracker` gives it
  only decoded `dict` chunks, and an unhashable reasoning `index` is ignored. Ingestion compacts the reply like other payloads (inline base64 and the
  oversize replacement).
- Retry and failover: a stream does not fail over after the first chunk is sent (RULES.md section 6).
  The tracker is created inside `stream_sse`, after the provider stream opens, so each served stream
  has one accumulator.
- Cancellation and errors: the reply is used only on the success path. `emit_stream_failure` does not
  change, so failed and cancelled streams have no response payload.

## Tests

New tests:

- `tests/test_stream_reply.py` (hermetic): text, reasoning, tool-call merge, finish reason, usage,
  other choices ignored, character limit, tool-call limit, unbounded mode, no chunks, fallback model,
  limits on tool-call identity fields and reasoning details, unhashable reasoning index, refusal.
- `tests/test_stream_audit_reply.py` (app):
  - The audit stores the reply and tokens for `/v1/chat/completions`, `/v1/responses`,
    `/v1/completions` and `/v1/messages` (translated).
  - A native Anthropic `/v1/messages` stream keeps thinking and `tool_use`.
  - The reply follows the audit payload limit and is marked `truncated`.
  - A stream without an audit service still works.

Commands and results on macOS (Python 3.12, no PostgreSQL, Redis or Helm):

```bash
uv sync --frozen --extra dev
uv run prisma generate --schema=./prisma/schema.prisma
uv run ruff check <touched files>          # All checks passed
uv run ruff format --check <touched files> # 8 files already formatted
git diff --check                           # clean
uv run pytest tests/test_stream_reply.py tests/test_stream_audit_reply.py \
  tests/test_stream_accounting_commit.py tests/test_text_endpoints.py tests/test_messages_native.py \
  tests/test_messages_endpoint.py tests/test_chat.py tests/test_stream_usage.py tests/mcp \
  tests/test_data_plane_audit_extended.py tests/test_audit_api.py tests/services/test_audit_service.py \
  tests/test_responses*.py -n 8              # 459 passed
uv run pytest tests -m "app or hermetic" -n 8 # see the note below
```

Note about the full `app` and `hermetic` lanes: the xdist run stopped at 97% because
`tests/providers/test_upstream_transport_tcp.py` does not end on this machine. That file also does
not end without this change (checked with `git stash`), so the problem is local. All other files in
these two lanes were run one file at a time: zero failures.

Not run: the `postgres`, `redis` and `helm` lanes. This change does not touch SQL, Redis or charts.

## Open questions for the maintainers

1. **Payload format.** The stream payload has the `chat.completion` format for all endpoints. A
   non-streamed `/v1/messages` request stores an Anthropic `message`. Options:
   - Keep `chat.completion` for streams (current change). It is one stable format, and the `stream`
     field identifies it.
   - Give the rebuilt body to the `response_transform` of the endpoint
     (`chat_response_to_anthropic_response` and others), so streams and non-streams match for each
     endpoint. The transforms drop `reasoning_content` (Anthropic) or need `ResponsesShape`. Thus
     this is more work and loses data.
2. **Opt-in setting.** The change is on when content storage is on for the organization. LiteLLM
   uses a separate opt-in. Is a `general_settings` flag necessary? If yes, update all configuration
   surfaces of RULES.md section 13 (config, example config, Helm values and schema, docs).
3. **Callbacks and guardrails.** `build_standard_logging_payload`, `execute_post_call_success_hooks`
   and `run_post_call_success` still get the placeholder for streams. External logging integrations
   (Langfuse and others) have the same problem. A follow-up can give the same body to them. This
   changes a plugin-visible contract, so it is not in this change.
4. **MCP streams.** `MCPStreamSession._events` calls `emit_stream_success` without a reply. The
   final hop's `MCPStreamHopBuffer` has the reply. All hops together, with the tool results, would
   be a better record. This needs a decision about the format.
5. **Character limit compared with byte limit.** `max_chars` uses `audit_payload_max_bytes` as a
   character count. For non-ASCII text, the JSON size can be larger. Ingestion then replaces the
   whole payload with the `payload_too_large` marker. A byte-exact limit costs more per line. The
   current limit prevents unbounded memory, and this is the important property.
6. **Accounting v2.** When accounting v2 owns the request, no text audit event is written, for
   streams and also for non-streams. This change keeps that behavior. A separate change can decide
   whether accounting v2 events carry payloads.

## Next steps

1. Review the diff: `git diff` and the two new test files.
2. Decide open questions 1 and 2 with the maintainers. An upstream issue can use the "Problem"
   section as its text.
3. Run the CI lanes (all five) on the pull request.
4. Optional: measure a streaming constant-arrival profile before and after the change (RULES.md
   section 7). The expected difference is close to zero, because the change adds no I/O.
5. Commit and open the pull request. Write the title and description in ASD-STE100 Simplified
   Technical English (RULES.md, "Documentation language").

## Files

| File | Change |
| --- | --- |
| `src/chat/stream_reply.py` | New. Shared `StreamReplyAccumulator`. |
| `src/chat/mcp_stream_buffer.py` | Delegates delta merging to the accumulator. No change in behavior. |
| `src/chat/stream_usage.py` | `StreamUsageTracker(reply=...)` gives decoded chunks to the accumulator. |
| `src/chat/audit.py` | `stream_reply_accumulator(request)`, `_audited_by_accounting`. |
| `src/chat/telemetry.py` | `emit_stream_success(reply=...)`; writes the reply and token columns. |
| `src/routers/chat.py` | Creates the accumulator and gives the rebuilt reply to the audit. |
| `docs/features/audit-log.md` | New section "Streamed responses". |
| `tests/test_stream_reply.py` | New hermetic tests. |
| `tests/test_stream_audit_reply.py` | New app tests. |
