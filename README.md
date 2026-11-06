# duckdb-mcp

**Let your AI agent run SQL over your local CSV, Parquet and JSON files, without giving it the keys to your disk.**

<!-- mcp-name: io.github.0xmortuex/duckdb-mcp -->

duckdb-mcp is an [MCP](https://modelcontextprotocol.io) server that points
[DuckDB](https://duckdb.org) at the data files in folders you choose. The agent lists the files,
looks at their columns, profiles them and runs SQL. It is built to be **safe by default**:

- **Read-only.** One SELECT-type statement per call. `INSERT`, `CREATE`, `COPY`, `ATTACH`,
  `INSTALL`/`LOAD`, `EXPORT`, `SET` and `PRAGMA` are rejected before they run.
- **Confined to the folders you allow.** DuckDB's own `allowed_directories` is set, external access
  is switched off and the configuration is locked, so `read_csv('C:/Windows/win.ini')`, `../`
  traversal, junction/symlink escapes, URLs and extension downloads all fail. This is tested, not assumed.
- **Bounded.** Results stop at 1000 rows (with a clear TRUNCATED notice) and every query has a
  30 second limit. Results are streamed, so `SELECT *` on a million-row file returns instantly.
- **Nothing to set up.** DuckDB runs in memory inside the server process. No database file, no
  service, no network.

## What it looks like

Real output from a stdio session against a small `orders.csv`:

```
> describe_file orders.csv
File: orders.csv  (csv, 180 B)
Rows: 5    Columns: 5

| column | type |
| --- | --- |
| order_id | BIGINT |
| customer | VARCHAR |
| country | VARCHAR |
| amount | DOUBLE |
| placed_at | DATE |

> query "SELECT country, count(*) AS orders, round(sum(amount),2) AS revenue FROM 'orders.csv' GROUP BY 1 ORDER BY 3 DESC"
| country | orders | revenue |
| --- | --- | --- |
| US | 2 | 380.0 |
| UK | 2 | 135.75 |
| FI | 1 | NULL |

3 rows, 3 columns.

> query "SELECT * FROM read_csv('C:/Users/me/secret.csv')"        (a file outside the allowed folders)
Error: Permission Error: Cannot access file "C:/Users/me/secret.csv" - file system operations are disabled by configuration
File access is limited to the allowed folders: C:/Users/me/data. Call list_files to see the queryable files.

> query "COPY (SELECT 1) TO 'x.csv'"
Error: COPY statements are blocked: this server is read-only. Allowed: SELECT / WITH / VALUES / FROM, DESCRIBE, SUMMARIZE, SHOW and EXPLAIN.
```

## Install

You need [uv](https://docs.astral.sh/uv/). Replace the path with the folder that holds your data.

**Claude Code**

```bash
claude mcp add duckdb -- uvx duckdb-mcp --dir "C:\Users\me\data"
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "duckdb": {
      "command": "uvx",
      "args": ["duckdb-mcp", "--dir", "C:\\Users\\me\\data"]
    }
  }
}
```

Or with pip: `pip install duckdb-mcp`, then run `duckdb-mcp --dir PATH`.

### Choosing the folders

| Option | Meaning |
|--------|---------|
| `--dir PATH` | A folder whose data files may be queried. Repeat it for several folders. |
| `DUCKDB_MCP_DIRS` | Same thing as an environment variable, separated by `;` on Windows and `:` elsewhere. Used when no `--dir` is given. |
| (nothing) | The server's current working directory. |
| `--max-rows N` | Row cap per result (default 1000). |
| `--timeout S` | Seconds a query may run (default 30). |

Subfolders are included. Files are named relative to their folder, or by absolute path.

## Tools

| Tool | What it does |
|------|--------------|
| `list_files` | Data files in the allowed folders, with type and size. Optional glob `pattern`. |
| `describe_file` | Column names and types, the row count and sample rows of one file. |
| `profile_file` | Per column: type, null %, approximate distinct count, min, max, average, median and the three most common values. |
| `sample_file` | The first N rows, or a random sample. |
| `query` | One read-only SQL statement. Output as a markdown table (default), CSV or JSON. |
| `explain_query` | The execution plan, optionally with run-time statistics (`analyze=true`). |

Supported files: `.csv`, `.tsv`, `.parquet`, `.json`, `.ndjson`, `.jsonl`, and `.gz` of the text
formats. In SQL, name a file as `FROM 'sales.csv'`, `read_csv('sales.csv')`,
`read_parquet('2024/*.parquet')` or `read_json_auto('events.ndjson')`. Joins across files work:
`SELECT * FROM 'a.csv' JOIN 'b.parquet' USING (id)`.

Excel (`.xlsx`) is not supported: DuckDB's Excel reader is an extension that has to be downloaded,
and this server deliberately turns extension loading off. Export the sheet to CSV first.

## Try asking

- "What data files do I have? Describe the biggest one."
- "Profile `orders.csv` and tell me which columns have data quality problems."
- "Which five customers spent the most last quarter?"
- "Join `users.parquet` with `events.ndjson` and show daily active users for March."
- "Is the `status` column ever empty? How many distinct values does it have?"
- "Explain why this query is slow" (it will use `explain_query`).

## Security

What is enforced, and by what:

| Threat | Defence |
|--------|---------|
| Reading files outside the allowed folders (`C:/Windows/...`, `/etc/passwd`, `../..`, UNC paths, a sibling folder with the same name prefix) | DuckDB `allowed_directories` + `enable_external_access=false`. DuckDB resolves `..` and junctions/symlinks, so those are refused too. |
| Network access (`read_csv('https://...')`, `INSTALL httpfs`) | `enable_external_access=false`; extension auto-install and auto-load are off. |
| Undoing the above from SQL | The statement gate rejects `SET`. Independently, DuckDB refuses to re-enable external access or change `allowed_directories` once access is off, and `lock_configuration=true` freezes every other setting. |
| Writing files (`COPY ... TO`, `EXPORT DATABASE`, `EXPLAIN ANALYZE COPY ...`) | DuckDB still allows writes inside allowed folders, so the statement gate only accepts one SELECT-type statement, and an `EXPLAIN` must wrap a SELECT. |
| Changing or creating objects (`CREATE`, `INSERT`, `ATTACH`, `PRAGMA`) | Same gate. |
| A runaway query | Interrupted after `--timeout` seconds. |
| Flooding the model's context | Row cap, and cells longer than 200 characters are shortened. |

What it does **not** do:

- It cannot stop a query from using CPU and memory up to the time limit (DuckDB's memory spill goes
  to a private temp folder).
- An allowed folder is readable in full. Do not allow a folder that holds secrets.
- For convenience, a quoted literal that names an existing data file in an allowed folder is
  replaced by that file's full path before the query runs. So `WHERE name = 'orders.csv'` compares
  against the full path when `orders.csv` exists. If you need the plain text, build it:
  `'orders' || '.csv'`. Reads are always enforced by DuckDB, whatever the rewrite does.
- There are no per-user accounts; whoever can talk to the server can query the allowed folders.

## Development

```bash
uv sync --extra test
uv run ruff check src tests && uv run mypy --strict src
uv run pytest tests
```

Tests use real files and real DuckDB: the sandbox suite attempts the escapes listed above, a
1,000,000-row CSV/Parquet test checks streaming and the row cap (and measures peak memory in a
child process), and a stdio test launches the installed server and talks MCP to it.

Want to know how it works? [duckdb-mcp-the-hard-way](https://github.com/0xmortuex/duckdb-mcp-the-hard-way)
builds a minimal version step by step.

## License

MIT
