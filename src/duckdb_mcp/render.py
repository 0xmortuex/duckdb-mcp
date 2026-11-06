"""Turn query results into compact text an LLM can read."""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from typing import Any

from .sandbox import QueryResult

MAX_CELL = 200


def cell(value: Any) -> str:
    """One value as display text. NULL is shown as NULL; long text is cut with '...'."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray)):
        head = bytes(value[:16]).hex()
        return f"0x{head}..." if len(value) > 16 else f"0x{head}"
    text = str(value)
    if len(text) > MAX_CELL:
        return text[:MAX_CELL] + f"... [{len(text) - MAX_CELL} more chars]"
    return text


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def markdown_table(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    head = "| " + " | ".join(_md(c) for c in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_md(cell(v)) for v in row) + " |" for row in rows]
    return "\n".join([head, sep, *body])


def to_csv(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for row in rows:
        w.writerow(["" if v is None else cell(v) for v in row])
    return buf.getvalue().rstrip("\n")


def to_json(res: QueryResult, cap: int) -> str:
    records = [dict(zip(res.columns, row)) for row in res.rows]
    payload: dict[str, Any] = {"row_count": len(records), "truncated": res.truncated}
    if res.truncated:
        payload["row_cap"] = cap
    payload["rows"] = records
    return json.dumps(payload, default=_json_default, ensure_ascii=False)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return cell(value)
    return str(value)


def format_result(res: QueryResult, fmt: str, cap: int) -> str:
    """Render a result in 'markdown', 'csv' or 'json', with a truncation notice."""
    n = len(res.rows)
    if fmt == "json":
        return to_json(res, cap)
    if fmt not in ("markdown", "csv"):
        raise ValueError(f"format must be markdown, csv or json (got {fmt!r})")
    if not res.columns:
        return "(statement returned no result set)"
    body = markdown_table(res.columns, res.rows) if fmt == "markdown" else to_csv(res.columns, res.rows)
    if res.truncated:
        notice = (
            f"TRUNCATED: showing the first {n} rows; the query returned more (row cap {cap}). "
            "Add WHERE/LIMIT, aggregate with GROUP BY, or select fewer columns."
        )
    else:
        notice = f"{n} row{'s' if n != 1 else ''}, {len(res.columns)} column{'s' if len(res.columns) != 1 else ''}."
    if fmt == "csv":
        return body + "\n# " + notice
    return body + "\n\n" + notice
