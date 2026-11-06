"""The six tools and the output formatting, called directly on real files."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from duckdb_mcp import server
from duckdb_mcp.sandbox import SandboxError


@pytest.fixture(autouse=True)
def configured(world: Path) -> Iterator[None]:
    server.configure([str(world / "data")], max_rows=20, timeout=10)
    yield
    server._box = None


def test_list_files(world: Path) -> None:
    out = server.list_files()
    for name in ("people.csv", "scores.tsv", "events.ndjson", "doc.json", "sub/nested.csv", "people.parquet"):
        assert name in out
    assert "notes.txt" not in out
    assert "| csv |" in out and "| parquet |" in out
    assert "6 files." in out


def test_list_files_pattern() -> None:
    out = server.list_files("*.CSV")
    assert "people.csv" in out and "sub/nested.csv" in out
    assert "scores.tsv" not in out
    assert "No data files" in server.list_files("*.xyz")


def test_describe_file() -> None:
    out = server.describe_file("people.csv")
    assert "Rows: 4" in out and "Columns: 4" in out
    assert "| id | BIGINT |" in out and "| name | VARCHAR |" in out
    assert "Ada" in out and "NULL" in out
    assert "First 4 rows" in out


def test_describe_parquet_and_json() -> None:
    assert "Rows: 100" in server.describe_file("people.parquet", sample_rows=0)
    out = server.describe_file("events.ndjson")
    assert "kind" in out and "Rows: 3" in out


def test_profile_file() -> None:
    out = server.profile_file("people.csv")
    lines = {ln.split("|")[1].strip(): ln for ln in out.splitlines() if ln.startswith("| ") and "---" not in ln}
    assert "Rows: 4" in out
    assert "25%" in lines["age"]  # one null of four
    assert "25%" in lines["city"]
    assert "0%" in lines["id"]
    assert "Ada" in lines["name"]
    only = server.profile_file("people.csv", columns=["age"])
    assert "age" in only and "city" not in only


def test_profile_file_with_nested_column(world: Path) -> None:
    (world / "data" / "nest.json").write_text('[{"a": 1, "s": {"x": 1}, "l": [1, 2]}]', encoding="utf-8")
    out = server.profile_file("nest.json")
    assert "| s |" in out and "| l |" in out


def test_sample_file() -> None:
    assert server.sample_file("people.parquet", rows=3).count("\n") >= 4
    assert "3 rows" in server.sample_file("people.parquet", rows=3)
    assert "5 rows" in server.sample_file("people.parquet", rows=5, mode="random")


def test_query_markdown() -> None:
    out = server.query("SELECT city, age FROM 'people.csv' WHERE age > 30 ORDER BY age")
    assert out.startswith("| city | age |")
    assert "| London | 36 |" in out
    assert out.endswith("2 rows, 2 columns.")


def test_query_csv_and_json() -> None:
    csv_out = server.query("SELECT id, name FROM 'people.csv' ORDER BY id", format="csv")
    assert csv_out.startswith("id,name\n1,Ada\n")
    data = json.loads(server.query("SELECT id, name, age FROM 'people.csv' ORDER BY id", format="json"))
    assert data["row_count"] == 4 and data["truncated"] is False
    assert data["rows"][2] == {"id": 3, "name": "Linus", "age": None}


def test_query_truncation_notice() -> None:
    out = server.query("SELECT * FROM range(1000)")
    assert "TRUNCATED" in out and "first 20 rows" in out
    data = json.loads(server.query("SELECT * FROM range(1000)", format="json"))
    assert data["truncated"] is True and data["row_count"] == 20 and data["row_cap"] == 20
    assert "TRUNCATED" in server.query("SELECT * FROM range(1000)", format="csv")


def test_query_max_rows() -> None:
    out = server.query("SELECT * FROM range(1000)", max_rows=3)
    assert "first 3 rows" in out


def test_markdown_escapes_pipes_and_newlines(world: Path) -> None:
    out = server.query("SELECT 'a|b' AS x, 'l1' || chr(10) || 'l2' AS y")
    assert "a\\|b" in out and "l1 l2" in out


def test_long_cells_are_shortened() -> None:
    out = server.query("SELECT repeat('x', 1000) AS big")
    assert "more chars" in out and len(out) < 500


def test_bad_format_rejected() -> None:
    with pytest.raises(ValueError, match="format"):
        server.query("SELECT 1", format="xml")  # type: ignore[arg-type]


def test_explain_query() -> None:
    out = server.explain_query("SELECT count(*) FROM 'people.parquet' WHERE n > 5")
    assert "PARQUET" in out.upper()
    analyzed = server.explain_query("SELECT count(*) FROM 'people.parquet'", analyze=True)
    assert "Total Time" in analyzed or "Query Profiling" in analyzed or "rows" in analyzed.lower()


def test_explain_query_rejects_writes(world: Path) -> None:
    target = world / "data" / "x.csv"
    with pytest.raises(SandboxError):
        server.explain_query(f"COPY (SELECT 1) TO '{target.as_posix()}'", analyze=True)
    assert not target.exists()


def test_describe_missing_and_outside(world: Path) -> None:
    with pytest.raises(SandboxError, match="list_files"):
        server.describe_file("absent.csv")
    with pytest.raises(SandboxError, match="outside the allowed folders"):
        server.describe_file((world / "outside" / "secret.csv").as_posix())
    with pytest.raises(SandboxError):
        server.describe_file("../outside/secret.csv")


def test_describe_unsupported_extension() -> None:
    with pytest.raises(SandboxError):
        server.describe_file("notes.txt")


def test_argument_validation() -> None:
    with pytest.raises(ValueError):
        server.sample_file("people.csv", rows=0)
    with pytest.raises(ValueError):
        server.describe_file("people.csv", sample_rows=99)


def test_cli_dirs_env_and_default(world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: started.append(list(server.box().dirs)))
    server.main(["--dir", str(world / "data"), "--dir", str(world / "data2")])
    assert [Path(d).name for d in started[-1]] == ["data", "data2"]
    monkeypatch.setenv("DUCKDB_MCP_DIRS", str(world / "data2"))
    server.main([])
    assert [Path(d).name for d in started[-1]] == ["data2"]
    monkeypatch.delenv("DUCKDB_MCP_DIRS")
    monkeypatch.chdir(world / "outside")
    server.main([])
    assert [Path(d).name for d in started[-1]] == ["outside"]
    with pytest.raises(SystemExit, match="does not exist"):
        server.main(["--dir", str(world / "nope")])
