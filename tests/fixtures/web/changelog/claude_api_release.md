# Claude API Release Notes

This page documents changes to the Claude API across all SDKs and HTTP endpoints.

## February 5, 2026

### Tool use improvements

We've improved the tool use experience with better error messages when a tool
call fails to parse. Errors now include the offending JSON snippet and the
specific schema validation failure.

- Tool errors now expose `details.schema_path` so callers can locate the field
  that failed validation.
- Streaming tool deltas now include explicit `partial_json` fragments, replacing
  the previous concatenation behavior that occasionally produced invalid JSON
  intermediate states.
- Improved retry semantics for transient tool errors when running through the
  Messages API batch endpoint.

We also expanded the supported list of MIME types accepted as document
attachments, including `application/x-yaml` and `text/x-rst`.

## January 22, 2026

### New model: claude-opus-4-7

`claude-opus-4-7-20260122` is now available. This release includes:

- 1M token context window in beta (`anthropic-beta: context-1m-2025-08-07`).
- Improved long-form coding performance on agentic benchmarks.
- Better handling of multi-file refactors when called through the Messages API.

Pricing remains unchanged from claude-opus-4-6.

## January 8, 2026

### Prompt caching now generally available

Prompt caching has graduated from beta to GA. Caching read units are billed at
1/10th the input token price and caching write units at 1.25x the input price.

- The `cache_control` parameter is now a stable part of the Messages API.
- Default cache TTL is 5 minutes; explicit 1-hour TTL via the
  `extra-cache-ttl-2025-04-11` beta header.
- Visible in the Anthropic Console usage dashboard under "Cache hit rate".

For migration guidance, see the prompt caching guide.
