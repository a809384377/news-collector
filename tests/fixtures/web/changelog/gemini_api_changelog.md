# Gemini API Changelog

This page tracks updates to the Gemini API and SDKs.

## 2026-05-07

### New: gemini-2.5-flash

`gemini-2.5-flash` is now generally available with improved long-context recall
and a 30% reduction in median latency versus 2.5-flash-preview. The model is
priced identically to 2.0-flash and supports the full multimodal input set —
text, images, audio, video and PDF — up to a 1M context window.

- Function calling: now supports parallel calls in a single response.
- Structured output: `response_schema` accepts `$ref` for nested schemas.
- Code execution: longer-running scripts (up to 60 seconds wallclock).

## 2026-04-22

### Embeddings model `text-embedding-005`

A new embedding model `text-embedding-005` is available. It produces 768-dim
vectors and supports task-type conditioning (`RETRIEVAL_DOCUMENT`,
`RETRIEVAL_QUERY`, `CLASSIFICATION`, `CLUSTERING`, `SEMANTIC_SIMILARITY`).
Pricing is $0.0001 per 1k input tokens.

## 2026-03-30

### Files API quota increase

The Files API now allows up to 50 GB of stored files per project (up from 20 GB)
and increases the per-file size limit from 2 GB to 4 GB. Files continue to be
retained for 48 hours by default; the `ttl` parameter on upload accepts up to
7 days for paid tier projects.
