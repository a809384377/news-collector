# Mixed Date Format Changelog

This fixture exercises the date-heading regex across all supported formats.

## 2026-05-07

### ISO format heading

This section uses the ISO `YYYY-MM-DD` heading format. We added a new endpoint
for streaming partial responses with explicit chunk boundaries, and tightened
the validation rules for the `metadata` field on Messages requests.

- Added: `/v1/messages/stream` endpoint with newline-delimited JSON output.
- Changed: `metadata.user_id` now rejects values longer than 256 characters.

## May 7, 2026

### English long-form heading

This section uses the English `Month D, YYYY` format. Improvements landed for
the batch processing pipeline: throughput is roughly 2x higher on workloads
with mixed model usage, and per-job error reports now include line numbers.

## May 7th, 2026

### English ordinal heading

This section uses the English ordinal form `May 7th, 2026`. We confirmed that
the regex matches `1st`, `2nd`, `3rd`, `4th` etc. The body content here is just
a placeholder to ensure the regex captures the heading correctly and yields the
right body slice up to the next date heading.

## **May 7, 2026**

This section uses bold-wrapped heading syntax. Notice there is no H3 subheading
in this section, so the title should fall back to `Update May 7, 2026`. The
body itself remains intact, including the bullets below.

- Bullet one: bold-wrapped headings should be tolerated.
- Bullet two: title fallback should kick in for sections without an H3.

## 5/7/2026

### Slash format heading

This section uses the US `M/D/YYYY` slash format. We continue to support this
form for legacy compatibility with older release notes pages that have been
migrated. Body content here also exceeds a few hundred characters to prevent
the page-level emptiness check from triggering on the fixture as a whole.
