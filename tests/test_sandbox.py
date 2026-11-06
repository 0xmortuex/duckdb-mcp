"""The safety contract, tested against a real DuckDB and real files."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import pytest
from conftest import make_junction

from duckdb_mcp.sandbox import Sandbox, SandboxError


def rows(box: Sandbox, sql: str) -> list[tuple[object, ...]]:
    return box.run(sql).rows


# ----------------------------------------------------------------- reading


def test_bare_file_name(box: Sandbox) -> None:
    assert rows(box, "SELECT count(*) FROM 'people.csv'") == [(4,)]


def test_read_csv_relative_and_subfolder(box: Sandbox) -> None:
    assert rows(box, "SELECT v FROM read_csv('sub/nested.csv') ORDER BY k") == [(1,), (2,)]


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT sum(score) FROM 'scores.tsv'", 17),
        ("SELECT count(*) FROM 'events.ndjson'", 3),
        ("SELECT sum(a) FROM 'doc.json'", 3),
        ("SELECT sum(n) FROM 'people.parquet'", 4950),
        ("SELECT sum(n) FROM read_parquet('people.parquet')", 4950),
    ],
)
def test_every_format(box: Sandbox, sql: str, expected: int) -> None:
    assert rows(box, sql) == [(expected,)]


def test_gzip_csv(box: Sandbox, world: Path) -> None:
    import gzip

    with gzip.open(world / "data" / "zipped.csv.gz", "wt", encoding="utf-8") as fh:
        fh.write("a,b\n1,2\n3,4\n")
    assert rows(box, "SELECT sum(b) FROM 'zipped.csv.gz'") == [(6,)]


def test_glob_inside(box: Sandbox) -> None:
    assert rows(box, "SELECT count(*) FROM read_csv('*.tsv')") == [(2,)]


def test_absolute_path_inside(box: Sandbox, world: Path) -> None:
    p = (world / "data" / "people.csv").as_posix()
    assert rows(box, f"SELECT count(*) FROM read_csv('{p}')") == [(4,)]


def test_windows_backslash_path_inside(box: Sandbox, world: Path) -> None:
    p = str(world / "data" / "people.csv")
    assert rows(box, f"SELECT count(*) FROM read_csv('{p}')") == [(4,)]


def test_literal_that_is_not_an_existing_file_is_untouched(box: Sandbox) -> None:
    assert rows(box, "SELECT 'absent.csv' AS s") == [("absent.csv",)]
    assert rows(box, "SELECT 'plain text' AS s") == [("plain text",)]
    assert box.rewrite_paths("SELECT 'it''s.csv'") == "SELECT 'it''s.csv'"


def test_describe_and_summarize_pass_the_gate(box: Sandbox) -> None:
    assert rows(box, "DESCRIBE SELECT * FROM 'people.csv'")[0][0] == "id"
    assert rows(box, "SUMMARIZE 'people.csv'")[0][0] == "id"
    assert rows(box, "WITH t AS (SELECT 1 AS x) SELECT x FROM t") == [(1,)]
    assert rows(box, "FROM 'people.csv' SELECT count(*)") == [(4,)]
    assert rows(box, "VALUES (1), (2)") == [(1,), (2,)]


def test_missing_file_message(box: Sandbox) -> None:
    with pytest.raises(SandboxError) as e:
        box.run("SELECT * FROM 'nothing.csv'")
    assert "list_files" in str(e.value)


def test_sql_syntax_error_is_a_sandbox_error(box: Sandbox) -> None:
    with pytest.raises(SandboxError, match="syntax"):
        box.run("SELEC 1")


# ---------------------------------------------------------------- sandbox


def outside(world: Path) -> str:
    return (world / "outside" / "secret.csv").as_posix()


def test_absolute_path_outside_is_denied(box: Sandbox, world: Path) -> None:
    with pytest.raises(SandboxError, match="allowed folders"):
        box.run(f"SELECT * FROM read_csv('{outside(world)}')")
    with pytest.raises(SandboxError, match="allowed folders"):
        box.run(f"SELECT * FROM '{outside(world)}'")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows system file")
def test_windows_system_file_is_denied(box: Sandbox) -> None:
    for fn in ("read_csv", "read_text", "read_blob"):
        with pytest.raises(SandboxError, match="allowed folders"):
            box.run(f"SELECT * FROM {fn}('C:/Windows/win.ini')")
    with pytest.raises(SandboxError, match="allowed folders"):
        box.run("SELECT * FROM glob('C:/Windows/*')")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX system file")
def test_posix_system_file_is_denied(box: Sandbox) -> None:
    with pytest.raises(SandboxError, match="allowed folders"):
        box.run("SELECT * FROM read_text('/etc/passwd')")


def test_dot_dot_traversal_is_denied(box: Sandbox, world: Path) -> None:
    base = (world / "data").as_posix()
    for sql in (
        "SELECT * FROM read_csv('../outside/secret.csv')",
        "SELECT * FROM '../outside/secret.csv'",
        f"SELECT * FROM read_csv('{base}/../outside/secret.csv')",
        f"SELECT * FROM read_csv('{base}/sub/../../outside/secret.csv')",
        "SELECT * FROM read_csv('sub/../../outside/secret.csv')",
    ):
        with pytest.raises(SandboxError):
            box.run(sql)


def test_backslash_traversal_is_denied(box: Sandbox, world: Path) -> None:
    p = str(world / "data") + "\\..\\outside\\secret.csv"
    with pytest.raises(SandboxError):
        box.run(f"SELECT * FROM read_csv('{p}')")


def test_sibling_folder_with_same_prefix_is_denied(box: Sandbox, world: Path) -> None:
    p = (world / "data2" / "neighbour.csv").as_posix()
    with pytest.raises(SandboxError, match="allowed folders"):
        box.run(f"SELECT * FROM read_csv('{p}')")


def test_glob_cannot_list_outside(box: Sandbox, world: Path) -> None:
    out = (world / "outside").as_posix()
    with pytest.raises(SandboxError):
        box.run(f"SELECT * FROM glob('{out}/*')")
    with pytest.raises(SandboxError):
        box.run(f"SELECT * FROM read_csv('{out}/*.csv')")


def test_junction_out_of_the_folder_is_denied(box: Sandbox, world: Path) -> None:
    link = world / "data" / "escape"
    if not make_junction(link, world / "outside"):
        pytest.skip("cannot create a directory link here")
    with pytest.raises(SandboxError):
        box.run("SELECT * FROM 'escape/secret.csv'")
    with pytest.raises(SandboxError):
        box.run(f"SELECT * FROM read_csv('{(link / 'secret.csv').as_posix()}')")


def test_urls_are_denied(box: Sandbox) -> None:
    with pytest.raises(SandboxError):
        box.run("SELECT * FROM read_csv('https://example.com/x.csv')")


def test_second_allowed_folder(world: Path) -> None:
    sb = Sandbox([str(world / "data"), str(world / "data2")])
    assert sb.run("SELECT * FROM 'neighbour.csv'").rows == [(1,)]
    with pytest.raises(SandboxError, match="allowed folders"):
        sb.run(f"SELECT * FROM '{outside(world)}'")


def test_same_name_in_two_folders_is_ambiguous(world: Path) -> None:
    (world / "data2" / "people.csv").write_text("id\n1\n", encoding="utf-8")
    sb = Sandbox([str(world / "data"), str(world / "data2")])
    with pytest.raises(SandboxError, match="more than one"):
        sb.run("SELECT * FROM 'people.csv'")


def test_configuration_is_locked_at_the_duckdb_level(box: Sandbox) -> None:
    """Even bypassing the SQL gate, DuckDB refuses to loosen the sandbox."""
    con = box._con
    for stmt in (
        "SET enable_external_access = true",
        "SET allowed_directories = []",
        "RESET allowed_directories",
        "SET lock_configuration = false",
    ):
        with pytest.raises(duckdb.Error):
            con.execute(stmt)


def test_missing_allowed_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="does not exist"):
        Sandbox([str(tmp_path / "nope")])


# ----------------------------------------------------------- statement gate


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t AS SELECT 1",
        "CREATE TEMP TABLE t(a INT)",
        "CREATE VIEW v AS SELECT 1",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN b INT",
        "ATTACH ':memory:' AS m",
        "DETACH m",
        "INSTALL httpfs",
        "LOAD httpfs",
        "COPY (SELECT 1) TO 'out.csv'",
        "COPY t FROM 'people.csv'",
        "EXPORT DATABASE 'dump'",
        "SET threads = 1",
        "RESET threads",
        "PRAGMA version",
        "CALL pragma_version()",
        "USE memory",
        "CHECKPOINT",
        "VACUUM",
        "PIVOT 'people.csv' ON city USING count(*)",
    ],
)
def test_blocked_statements(box: Sandbox, sql: str) -> None:
    with pytest.raises(SandboxError):
        box.run(sql)


def test_blocked_statement_message_says_why(box: Sandbox) -> None:
    with pytest.raises(SandboxError, match="read-only"):
        box.run("DROP TABLE t")


def test_multiple_statements_rejected(box: Sandbox) -> None:
    with pytest.raises(SandboxError, match="exactly one"):
        box.run("SELECT 1; SELECT 2")
    with pytest.raises(SandboxError, match="exactly one"):
        box.run("SELECT 1; DROP TABLE t")


def test_empty_sql_rejected(box: Sandbox) -> None:
    with pytest.raises(SandboxError, match="empty"):
        box.run("   ")


def test_explain_of_select_is_allowed(box: Sandbox) -> None:
    res = box.run("EXPLAIN SELECT * FROM 'people.csv'")
    assert "READ_CSV" in "".join(str(c) for r in res.rows for c in r).upper()


def test_explain_analyze_copy_cannot_write(box: Sandbox, world: Path) -> None:
    """EXPLAIN ANALYZE executes its statement; DuckDB would happily write the file."""
    target = world / "data" / "pwned.csv"
    for sql in (
        f"EXPLAIN ANALYZE COPY (SELECT 1) TO '{target.as_posix()}'",
        f"EXPLAIN (ANALYZE) COPY (SELECT 1) TO '{target.as_posix()}'",
        f"explain analyze   copy (select 1) to '{target.as_posix()}'",
    ):
        with pytest.raises(SandboxError, match="EXPLAIN"):
            box.run(sql)
    assert not target.exists()


def test_nothing_was_written_by_any_blocked_statement(box: Sandbox, world: Path) -> None:
    before = sorted(p.name for p in (world / "data").iterdir())
    for sql in ("COPY (SELECT 1) TO 'x.csv'", "EXPORT DATABASE 'dump'", "CREATE TABLE t AS SELECT 1"):
        with pytest.raises(SandboxError):
            box.run(sql)
    assert sorted(p.name for p in (world / "data").iterdir()) == before


def test_query_function_cannot_smuggle_a_write(box: Sandbox, world: Path) -> None:
    target = (world / "data" / "smuggled.csv").as_posix()
    with pytest.raises(SandboxError):
        box.run(f"SELECT * FROM query('COPY (SELECT 1) TO ''{target}''')")
    assert not Path(target).exists()


# ------------------------------------------------------------ caps and time


def test_row_cap_and_truncation_flag(box: Sandbox) -> None:
    res = box.run("SELECT * FROM range(1000000)")
    assert len(res.rows) == 50 and res.truncated


def test_exactly_at_cap_is_not_truncated(box: Sandbox) -> None:
    res = box.run("SELECT * FROM range(50)")
    assert len(res.rows) == 50 and not res.truncated


def test_smaller_max_rows_and_cannot_exceed_server_cap(box: Sandbox) -> None:
    assert len(box.run("SELECT * FROM range(100)", max_rows=5).rows) == 5
    assert len(box.run("SELECT * FROM range(100)", max_rows=10_000).rows) == 50


def test_timeout_stops_a_runaway_query(world: Path) -> None:
    sb = Sandbox([str(world / "data")], timeout=0.5)
    start = time.monotonic()
    with pytest.raises(SandboxError, match="time limit"):
        sb.run("SELECT count(*) FROM range(100000000) a, range(100000000) b")
    assert time.monotonic() - start < 10
    # the sandbox is still usable afterwards
    assert sb.run("SELECT 1").rows == [(1,)]
