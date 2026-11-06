"""The safety core: a locked-down in-memory DuckDB plus SQL gating.

Two independent layers keep a query away from things it should not touch:

1. DuckDB itself. After setup the connection has ``allowed_directories`` set to the
   folders the user chose, ``enable_external_access`` turned off (no network, no
   extensions, no files outside the allowed folders) and ``lock_configuration`` on,
   so SQL cannot undo any of it.
2. A statement gate. DuckDB still lets ``COPY ... TO`` write inside an allowed folder,
   so only a single SELECT-type statement (SELECT, WITH, VALUES, FROM, DESCRIBE,
   SUMMARIZE, SHOW) or EXPLAIN of one is accepted.
"""

from __future__ import annotations

import glob
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

DATA_SUFFIXES = (".csv", ".tsv", ".parquet", ".json", ".ndjson", ".jsonl")
_COMPRESSED = (".csv", ".tsv", ".json", ".ndjson", ".jsonl")
GLOB_CHARS = "*?["

# A quoted literal in SQL: 'text' with '' as the escape, or "text" with "" as the escape.
_QUOTED = re.compile(r"'((?:[^']|'')*)'|\"((?:[^\"]|\"\")*)\"")
_EXPLAIN_PREFIX = re.compile(r"^\s*explain(?:\s+analyze\b)?(?:\s*\([^)]*\))?\s*", re.IGNORECASE)


class SandboxError(Exception):
    """An expected failure whose message is written for the agent that caused it."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool


def data_kind(path: str) -> str | None:
    """Return 'csv', 'tsv', 'parquet' or 'json' for a data file name, else None."""
    low = path.lower()
    if low.endswith(".gz"):
        low = low[:-3]
        if not low.endswith(_COMPRESSED):
            return None
    for suffix, kind in (
        (".csv", "csv"),
        (".tsv", "tsv"),
        (".parquet", "parquet"),
        (".json", "json"),
        (".ndjson", "json"),
        (".jsonl", "json"),
    ):
        if low.endswith(suffix):
            return kind
    return None


def sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def reader_sql(posix_path: str) -> str:
    """The table function that reads this file, e.g. read_parquet('...')."""
    kind = data_kind(posix_path)
    lit = sql_literal(posix_path)
    if kind == "parquet":
        return f"read_parquet({lit})"
    if kind == "json":
        return f"read_json_auto({lit})"
    if kind in ("csv", "tsv"):
        return f"read_csv({lit})"
    raise SandboxError(
        f"{posix_path!r} is not a supported data file. Supported: "
        + ", ".join(DATA_SUFFIXES)
        + " (each optionally .gz-compressed except .parquet)."
    )


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Sandbox:
    def __init__(self, dirs: list[str], max_rows: int = 1000, timeout: float = 30.0) -> None:
        if max_rows < 1:
            raise SandboxError("max_rows must be at least 1")
        if timeout <= 0:
            raise SandboxError("timeout must be positive")
        if not dirs:
            raise SandboxError("at least one allowed directory is required")
        self.dirs: list[str] = []
        for d in dirs:
            p = Path(d).expanduser()
            if not p.is_dir():
                raise SandboxError(f"allowed directory {d!r} does not exist or is not a directory")
            posix = p.resolve().as_posix().rstrip("/")
            if posix not in self.dirs:
                self.dirs.append(posix)
        self.max_rows = max_rows
        self.timeout = timeout
        # DuckDB spills big sorts/joins here; removed when the sandbox is collected or at exit.
        self._spill = tempfile.TemporaryDirectory(prefix="duckdb-mcp-")
        self._con = self._connect()

    # ------------------------------------------------------------------ setup

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(":memory:")
        # Spill files go to a private temp dir, not a ".tmp" folder in the working directory.
        con.execute("SET temp_directory = ?", [self._spill.name])
        con.execute("SET autoinstall_known_extensions = false")
        con.execute("SET autoload_known_extensions = false")
        # The trailing slash makes each entry a directory, not a bare name prefix.
        con.execute("SET allowed_directories = ?", [[d + "/" for d in self.dirs]])
        # Order matters: allowed_directories cannot be changed once access is off,
        # and nothing can be changed once the configuration is locked.
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        return con

    # ------------------------------------------------------------ path handling

    def inside(self, posix_path: str) -> bool:
        real = Path(posix_path).resolve().as_posix()
        return any(real == d or real.startswith(d + "/") for d in self.dirs)

    def resolve_path(self, name: str) -> str:
        """Turn a user-supplied path (absolute, or relative to an allowed folder) into an
        absolute forward-slash path inside an allowed folder. Raises SandboxError."""
        found = self._find(name)
        if found is None:
            if os.path.isabs(name):
                raise SandboxError(
                    f"{name!r} is outside the allowed folders ({', '.join(self.dirs)}) "
                    "or does not exist. Call list_files to see what can be queried."
                )
            raise SandboxError(
                f"No file matching {name!r} in the allowed folders ({', '.join(self.dirs)}). "
                "Call list_files to see what can be queried."
            )
        return found

    def _find(self, name: str) -> str | None:
        matches = self._candidates(name)
        if not matches:
            return None
        if len(matches) > 1:
            raise SandboxError(
                f"{name!r} exists in more than one allowed folder: {', '.join(matches)}. "
                "Use the full path."
            )
        return matches[0]

    def _candidates(self, name: str) -> list[str]:
        is_glob = any(ch in name for ch in GLOB_CHARS)
        if os.path.isabs(name):
            options = [name]
        else:
            options = [d + "/" + name for d in self.dirs]
        out: list[str] = []
        for opt in options:
            norm = os.path.normpath(opt).replace("\\", "/")
            if is_glob:
                hits = [h for h in glob.glob(norm) if os.path.isfile(h)]
                ok = bool(hits) and all(self.inside(h.replace("\\", "/")) for h in hits)
            else:
                ok = os.path.isfile(norm) and self.inside(norm)
            if ok:
                # keep a glob pattern as a pattern; DuckDB expands it
                out.append(norm)
        return out

    def rewrite_paths(self, sql: str) -> str:
        """Replace relative data-file names in quoted literals with absolute paths.

        'sales.csv' becomes 'C:/data/sales.csv' when that file exists in exactly one
        allowed folder. Literals that are not existing data files (a WHERE value, say)
        are left alone. This is a convenience only: DuckDB enforces the sandbox."""

        def repl(m: re.Match[str]) -> str:
            single = m.group(1) is not None
            body = m.group(1) if single else m.group(2)
            assert body is not None
            text = body.replace("''", "'") if single else body.replace('""', '"')
            if os.path.isabs(text) or not self._looks_like_data(text):
                return m.group(0)
            found = self._find(text)
            if found is None:
                return m.group(0)
            return sql_literal(found)

        return _QUOTED.sub(repl, sql)

    @staticmethod
    def _looks_like_data(text: str) -> bool:
        return data_kind(text) is not None and "\n" not in text

    # -------------------------------------------------------------- SQL gating

    def check_sql(self, sql: str) -> None:
        """Raise SandboxError unless sql is one read-only statement."""
        if not sql.strip():
            raise SandboxError("The SQL is empty.")
        statements = self._extract(sql)
        if len(statements) != 1:
            raise SandboxError(
                f"Send exactly one SQL statement per call (got {len(statements)})."
            )
        kind = statements[0].type.name
        if kind == "EXPLAIN":
            # EXPLAIN ANALYZE really executes its statement, including a COPY ... TO,
            # so judge the statement inside it.
            inner = _EXPLAIN_PREFIX.sub("", sql, count=1)
            inner_statements = self._extract(inner)
            if len(inner_statements) != 1 or inner_statements[0].type.name != "SELECT":
                raise SandboxError("EXPLAIN is only allowed on a SELECT query.")
            return
        if kind != "SELECT":
            raise SandboxError(
                f"{kind} statements are blocked: this server is read-only. Allowed: "
                "SELECT / WITH / VALUES / FROM, DESCRIBE, SUMMARIZE, SHOW and EXPLAIN. "
                "Files are read in the query itself, e.g. SELECT * FROM 'sales.csv'."
            )

    def _extract(self, sql: str) -> list[Any]:
        try:
            return list(self._con.extract_statements(sql))
        except duckdb.Error as e:
            raise SandboxError(f"SQL syntax error: {_clean(e)}") from e

    # --------------------------------------------------------------- execution

    def run(self, sql: str, max_rows: int | None = None) -> QueryResult:
        """Gate, resolve file names, execute with a timeout, and fetch at most max_rows."""
        cap = self.max_rows if max_rows is None else max_rows
        if cap < 1:
            raise SandboxError("max_rows must be at least 1")
        cap = min(cap, self.max_rows)
        sql = self.rewrite_paths(sql)
        self.check_sql(sql)
        cur = self._con.cursor()
        timed_out = threading.Event()

        def fire() -> None:
            timed_out.set()
            cur.interrupt()

        timer = threading.Timer(self.timeout, fire)
        timer.start()
        try:
            cur.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            # Fetch one more than the cap so truncation is known without a full scan.
            rows = cur.fetchmany(cap + 1)
        except duckdb.Error as e:
            if timed_out.is_set():
                raise SandboxError(
                    f"Query stopped after the {self.timeout:g}s time limit. Filter earlier "
                    "(WHERE), select fewer columns, or aggregate."
                ) from e
            raise SandboxError(self._explain_error(e)) from e
        finally:
            timer.cancel()
            cur.close()
        truncated = len(rows) > cap
        return QueryResult(columns, [tuple(r) for r in rows[:cap]], truncated)

    def _explain_error(self, e: duckdb.Error) -> str:
        text = _clean(e)
        if isinstance(e, duckdb.PermissionException) or "file system operations are disabled" in text:
            return (
                f"{text}\nFile access is limited to the allowed folders: "
                f"{', '.join(self.dirs)}. Call list_files to see the queryable files."
            )
        return text


def _clean(e: Exception) -> str:
    """DuckDB's own message, whitespace-trimmed."""
    return str(e).strip()
