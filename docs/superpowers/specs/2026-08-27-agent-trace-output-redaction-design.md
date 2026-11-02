# AgentTraceSDK Output Redaction Design

> 2026-11-02：模型 chat 2KB 摘要契约由
> `2026-11-02-agent-trace-sdk-022-design.md` 的结构化有界 I/O 替代。
> 本文件的工具输出限制继续有效。

## Goal

Enable meaningful, bounded `cmb.output.arguments` values for Main Agent
model and actual tool execution Spans without exporting credentials, tenant
environment values, raw high-risk tool output, or unbounded payloads.

## Scope

This change applies only to the parallel `trace_sdk` integration. The existing
`swe.tracing` pipeline, its storage schema, and its output handling remain
unchanged.

## Output Contract

Model Spans export only the following structure:

```json
{
  "role": "assistant",
  "text": "bounded and redacted response text",
  "text_truncated": false,
  "tool_call_names": ["read_file"]
}
```

Tool Spans export one of these structures:

```json
{
  "status": "ok",
  "output_preview": "bounded and redacted output",
  "output_bytes": 128,
  "truncated": false
}
```

```json
{
  "status": "ok",
  "output_bytes": 128,
  "truncated": false
}
```

The second form applies to shell commands, file-reading tools, and all MCP
tools unless a future explicit allowlist permits a preview. It deliberately
does not include an unkeyed output digest, because low-entropy output can be
identified by hash comparison. Error, empty, and timeout outcomes use `status`
values of `error`, `empty`, and `timeout`.

Never export `Msg.metadata`, attachments, provider raw responses, full tool
result blocks, tool invocation arguments, or arbitrary object fields.

## Redaction

The application sanitizes values before invoking `trace_sdk`; exporter
redaction remains a second defense layer.

The existing `swe.tracing.sanitizer` becomes the shared sanitization boundary:

1. Recursively redact values whose keys contain configured secret markers,
   including API keys, passwords, tokens, authorization, cookies, credentials,
   and private keys.
2. Replace tenant environment values already registered in the request context.
3. Redact secret-like text fragments embedded in strings: Authorization and
   Bearer headers, Cookie headers, sensitive `KEY=value` assignments, JWTs,
   PEM private-key blocks, and credentials embedded in connection URLs.
4. Bound strings by UTF-8 byte count and report truncation. Bound collection
   length, nesting depth, and emitted field count before serialization.

The sanitizer must preserve valid JSON-compatible types and never throw into
the business path. A sanitization failure returns a minimal status-only output
object and is logged by the tracing path.

## Execution Boundaries

`SWEAgent._run_reasoning_with_internal_context` returns a `Msg`, so its
`output_arguments_factory` can build the model structure directly from the
returned message.

`ToolGuardMixin._run_tool_call_with_hard_timeout` frequently returns `None`.
The actual terminal output is written by `ToolOutputBudgetMixin._acting` to
Agent memory. Tool output instrumentation must therefore obtain the terminal
result after execution from the matching tool-result memory block. It must not
change the existing `dict | None` return contract just to pass data to the
factory.

Use a private tracing-only outcome wrapper or an equivalent documented SDK
post-result mechanism to give the tool output factory both the original return
value and the terminal memory result. The public tool execution return value
remains unchanged.

## Verification

Unit tests cover:

- Model text, tool-call names, and truncation.
- Normal tool output read from memory when the business return value is `None`.
- Structured tool errors, timeouts, and empty results.
- Nested sensitive keys and registered tenant secret values.
- Bearer tokens, cookies, JWTs, PEM private keys, and connection-string
  credentials embedded in text.
- UTF-8 byte limits, nested structures, and high-risk-tool preview exclusion.
- Factories producing non-empty, JSON-compatible `cmb.output.arguments` data.

A real private-SDK Console Exporter smoke test verifies completed model and
tool Span JSON contains the expected output attribute, with no raw secret or
high-risk output preview.

## Documentation

Update `trace_design.md` to distinguish the SDK default output-capture rule
from Swe's output contract. Remove the previous claim that Swe intentionally
uses empty model and tool output factories once this design is implemented.
