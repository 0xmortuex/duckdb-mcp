# Changelog

## 0.1.0 - 2026-11-06

First release.

- Six tools: `list_files`, `describe_file`, `profile_file`, `query`, `sample_file`,
  `explain_query`.
- Reads CSV, TSV, Parquet, JSON and NDJSON/JSONL (and `.gz` of the text formats) through DuckDB.
- Safe by default: in-memory database, file access confined to `--dir` folders with DuckDB's
  `allowed_directories`, `enable_external_access` off and `lock_configuration` on; only a
  single SELECT-type statement (or EXPLAIN of one) is accepted, so COPY, ATTACH, INSTALL/LOAD,
  EXPORT, SET, PRAGMA, DDL and DML are rejected.
- Row cap (default 1000, `--max-rows`) with a TRUNCATED notice, per-query timeout
  (default 30 s, `--timeout`), results fetched as a stream.
- Output as markdown tables, or CSV / JSON.
