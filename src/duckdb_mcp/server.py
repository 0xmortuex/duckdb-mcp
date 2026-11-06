"""duckdb-mcp: query local data files with SQL through a locked-down DuckDB."""

from __future__ import annotations

import argparse
import fnmatch
import functools
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import render
from .sandbox import QueryResult, Sandbox, SandboxError, data_kind, quote_ident, reader_sql

DEFAULT_MAX_ROWS = 1000
DEFAULT_TIMEOUT = 30.0
LIST_LIMIT = 500
PROFILE_COLUMN_LIMIT = 50
_SKIP_DIRS = {"node_modules", "__pycache__", "$RECYCLE.BIN", "System Volume Information"}

mcp = MCPServer(
    "duckdb",
    instructions=(
        "Query local CSV, TSV, Parquet, JSON and NDJSON files with SQL (DuckDB). Access is "
        "read-only and limited to the folders this server was started with. Start with "
        "list_files, then describe_file to see a file's columns and sample rows, then query. "
        "In SQL, name a file like SELECT * FROM 'sales.csv' or read_csv('data/sales.csv') "
        "(paths are relative to the allowed folders). Results are capped; aggregate in SQL "
        "(GROUP BY, COUNT, AVG) instead of pulling raw rows."
    ),
)

_box: Sandbox | None = None

F = TypeVar("F", bound=Callable[..., Any])


def configure(dirs: Sequence[str], max_rows: int = DEFAULT_MAX_ROWS, timeout: float = DEFAULT_TIMEOUT) -> Sandbox:
    """Create the sandbox. Called once at startup (and by tests)."""
    global _box
    _box = Sandbox(list(dirs), max_rows=max_rows, timeout=timeout)
    return _box


def box() -> Sandbox:
    if _box is None:
        raise SandboxError("The server is not configured: no allowed folders were set.")
    return _box


# MCPServer only forwards a ToolError's message to the client; anything else arrives
# as a bare "Error executing tool X". SandboxError and ValueError carry messages written
# for the agent, so they are re-raised as ToolError. Genuine bugs stay unlisted.
_EXPECTED = (SandboxError, ValueError)


def tool(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except _EXPECTED as e:
            raise ToolError(str(e)) from e

    mcp.tool()(wrapper)
    return fn


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _table(res: QueryResult) -> str:
    return render.markdown_table(res.columns, res.rows)


@tool
def list_files(pattern: str = "") -> str:
    """List the data files you can query (CSV, TSV, Parquet, JSON, NDJSON, optionally .gz).

    Shows each file's path relative to its allowed folder, its type and size. Use this
    first: those paths can be used directly in SQL, e.g. SELECT * FROM 'sales.csv'.
    Optional `pattern` is a case-insensitive glob on the path, e.g. '*2024*' or 'logs/*.json'.
    At most 500 files are shown.
    """
    sb = box()
    rows: list[tuple[Any, ...]] = []
    total = 0
    for root in sb.dirs:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS)
            for name in sorted(filenames):
                kind = data_kind(name)
                if kind is None:
                    continue
                full = os.path.join(dirpath, name).replace("\\", "/")
                rel = full[len(root) + 1 :]
                if pattern and not fnmatch.fnmatch(rel.lower(), pattern.lower()):
                    continue
                total += 1
                if len(rows) < LIST_LIMIT:
                    rows.append((rel, kind, _fmt_size(os.path.getsize(full)), root))
    if not rows:
        extra = f" matching {pattern!r}" if pattern else ""
        return (
            f"No data files{extra} found in the allowed folders: {', '.join(sb.dirs)}. "
            "Supported: .csv .tsv .parquet .json .ndjson .jsonl (and .gz of the text formats)."
        )
    out = render.markdown_table(["path", "type", "size", "folder"], rows)
    note = f"\n\n{total} file{'s' if total != 1 else ''}."
    if total > LIST_LIMIT:
        note = f"\n\nShowing the first {LIST_LIMIT} of {total} files. Narrow it with `pattern`."
    return out + note


@tool
def describe_file(path: str, sample_rows: int = 5) -> str:
    """Show a file's schema (column names and types), its row count and a few sample rows.

    `path` is relative to an allowed folder (as list_files shows it) or absolute inside
    one. Column types are DuckDB's auto-detected types, which is what SQL will see. The
    row count of a CSV/JSON file needs a full scan; Parquet answers from metadata.
    Use this before writing a query, to get exact column names.
    """
    sb = box()
    if not 0 <= sample_rows <= 50:
        raise ValueError("sample_rows must be between 0 and 50")
    resolved = sb.resolve_path(path)
    src = reader_sql(resolved)
    schema = sb.run(f"DESCRIBE SELECT * FROM {src}")
    count = sb.run(f"SELECT count(*) FROM {src}")
    size = _fmt_size(os.path.getsize(resolved)) if "*" not in resolved and "?" not in resolved else "glob"
    parts = [
        f"File: {path}  ({data_kind(resolved)}, {size})",
        f"Rows: {count.rows[0][0]:,}    Columns: {len(schema.rows)}",
        "",
        render.markdown_table(
            ["column", "type"], [(r[0], r[1]) for r in schema.rows]
        ),
    ]
    if sample_rows:
        sample = sb.run(f"SELECT * FROM {src} LIMIT {sample_rows}")
        parts += ["", f"First {len(sample.rows)} rows:", _table(sample)]
    return "\n".join(parts)


_NESTED = ("STRUCT", "MAP", "UNION", "BLOB", "[]")


@tool
def profile_file(path: str, columns: list[str] | None = None) -> str:
    """Profile each column of a file in one pass: type, null %, approximate distinct
    count, min, max, average, median and the three most common values.

    Use it to spot data-quality problems (many nulls, constant columns, odd ranges)
    before analysing. `columns` limits it to named columns; otherwise the first 50
    columns are profiled. On a very large file this scans everything and can hit the
    time limit - then profile fewer columns.
    """
    sb = box()
    resolved = sb.resolve_path(path)
    src = reader_sql(resolved)
    if columns:
        select = ", ".join(quote_ident(c) for c in columns)
        base = f"SELECT {select} FROM {src}"
        note = ""
    else:
        names = [r[0] for r in sb.run(f"DESCRIBE SELECT * FROM {src}").rows]
        if len(names) > PROFILE_COLUMN_LIMIT:
            select = ", ".join(quote_ident(c) for c in names[:PROFILE_COLUMN_LIMIT])
            base = f"SELECT {select} FROM {src}"
            note = f"\n\nProfiled the first {PROFILE_COLUMN_LIMIT} of {len(names)} columns; pass `columns` for others."
        else:
            base = f"SELECT * FROM {src}"
            note = ""
    summary = sb.run(f"SUMMARIZE {base}")
    idx = {c: i for i, c in enumerate(summary.columns)}
    scalar = [
        r[idx["column_name"]]
        for r in summary.rows
        if not any(tok in str(r[idx["column_type"]]) for tok in _NESTED)
    ]
    tops: dict[str, str] = {}
    if scalar:
        exprs = ", ".join(f"approx_top_k({quote_ident(c)}, 3)" for c in scalar)
        top_row = sb.run(f"SELECT {exprs} FROM ({base})").rows[0]
        for name, vals in zip(scalar, top_row):
            tops[name] = ", ".join(render.cell(v) for v in (vals or []))
    rows = []
    for r in summary.rows:
        name = r[idx["column_name"]]
        rows.append(
            (
                name,
                r[idx["column_type"]],
                f"{float(r[idx['null_percentage']]):g}%",
                r[idx["approx_unique"]],
                r[idx["min"]],
                r[idx["max"]],
                r[idx["avg"]],
                r[idx["q50"]],
                tops.get(name, "-"),
            )
        )
    head = ["column", "type", "nulls", "distinct~", "min", "max", "avg", "median", "top values"]
    total = summary.rows[0][idx["count"]] if summary.rows else 0
    return f"Rows: {total:,}\n\n" + render.markdown_table(head, rows) + note


@tool
def sample_file(path: str, rows: int = 10, mode: Literal["head", "random"] = "head") -> str:
    """Show a few rows of a file: the first rows (mode='head') or a random sample
    (mode='random', scans the whole file). Cheaper than describe_file when you only
    want to see the data. `rows` is 1 to 100."""
    sb = box()
    if not 1 <= rows <= 100:
        raise ValueError("rows must be between 1 and 100")
    if mode not in ("head", "random"):
        raise ValueError("mode must be 'head' or 'random'")
    src = reader_sql(sb.resolve_path(path))
    if mode == "head":
        res = sb.run(f"SELECT * FROM {src} LIMIT {rows}")
    else:
        res = sb.run(f"SELECT * FROM {src} USING SAMPLE {rows} ROWS")
    return render.format_result(res, "markdown", rows)


@tool
def query(
    sql: str,
    format: Literal["markdown", "csv", "json"] = "markdown",
    max_rows: int | None = None,
) -> str:
    """Run one read-only SQL statement (DuckDB dialect) over the data files.

    Reference files by name: SELECT * FROM 'sales.csv', or read_csv('sales.csv'),
    read_parquet('dir/*.parquet'), read_json_auto('events.ndjson'). Paths are relative to
    the allowed folders (see list_files) or absolute inside them. Allowed: SELECT, WITH,
    VALUES, FROM-first queries, DESCRIBE, SUMMARIZE, SHOW and EXPLAIN. Anything that writes
    or reaches outside the allowed folders (INSERT, CREATE, COPY, ATTACH, INSTALL, SET,
    PRAGMA, files elsewhere on disk, URLs) is rejected.

    Output is a markdown table by default; format='csv' or 'json' is more compact for big
    results. Results stop at the server's row cap (default 1000) with a TRUNCATED notice;
    pass a smaller max_rows to see less. Queries have a time limit. Prefer aggregation
    (GROUP BY, COUNT, SUM) over fetching raw rows. Use describe_file first for column names.
    """
    sb = box()
    if format not in ("markdown", "csv", "json"):
        raise ValueError("format must be 'markdown', 'csv' or 'json'")
    cap = sb.max_rows if max_rows is None else min(max_rows, sb.max_rows)
    res = sb.run(sql, max_rows)
    return render.format_result(res, format, cap)


@tool
def explain_query(sql: str, analyze: bool = False) -> str:
    """Show DuckDB's execution plan for a SELECT query without returning its rows.

    Useful to check whether a filter is pushed down into a Parquet scan, or why a query is
    slow. With analyze=true the query is actually executed and per-operator timings and
    row counts are included (still read-only, still time-limited). Same SQL rules and file
    naming as `query`.
    """
    sb = box()
    sql = sb.rewrite_paths(sql)
    sb.check_sql(sql)
    prefix = "EXPLAIN ANALYZE " if analyze else "EXPLAIN "
    res = sb.run(prefix + sql.strip().rstrip(";"), max_rows=100)
    return "\n".join(str(r[-1]) for r in res.rows)


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="duckdb-mcp",
        description="MCP server: query local data files with SQL, read-only, inside allowed folders.",
    )
    p.add_argument(
        "--dir",
        action="append",
        metavar="PATH",
        help="folder whose data files may be queried (repeatable). Default: DUCKDB_MCP_DIRS "
        "(separated by the OS path separator), else the current directory.",
    )
    p.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS, help="row cap per result (default 1000)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per query (default 30)")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse(argv)
    dirs: list[str] = args.dir or []
    if not dirs:
        env = os.environ.get("DUCKDB_MCP_DIRS", "")
        dirs = [d for d in env.split(os.pathsep) if d]
    if not dirs:
        dirs = [os.getcwd()]
    try:
        configure(dirs, max_rows=args.max_rows, timeout=args.timeout)
    except SandboxError as e:
        sys.exit(f"duckdb-mcp: {e}")
    mcp.run()


if __name__ == "__main__":
    main()
