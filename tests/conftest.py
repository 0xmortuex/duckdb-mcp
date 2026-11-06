"""Shared fixtures: real data files written by DuckDB into a temp folder."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from duckdb_mcp.sandbox import Sandbox


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def world(tmp_path: Path) -> Path:
    """tmp/data (allowed), tmp/data2 (a sibling whose name starts like it), tmp/outside."""
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (tmp_path / "data2").mkdir()
    (tmp_path / "outside").mkdir()
    (data / "people.csv").write_text(
        "id,name,age,city\n1,Ada,36,London\n2,Grace,45,New York\n3,Linus,,Helsinki\n4,Zoe,29,\n",
        encoding="utf-8",
    )
    (data / "scores.tsv").write_text("who\tscore\nAda\t10\nZoe\t7\n", encoding="utf-8")
    (data / "events.ndjson").write_text(
        '{"id": 1, "kind": "click"}\n{"id": 2, "kind": "view"}\n{"id": 3, "kind": "click"}\n',
        encoding="utf-8",
    )
    (data / "doc.json").write_text('[{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]', encoding="utf-8")
    (data / "sub" / "nested.csv").write_text("k,v\na,1\nb,2\n", encoding="utf-8")
    (data / "notes.txt").write_text("not a data file", encoding="utf-8")
    con = duckdb.connect()
    p = (data / "people.parquet").as_posix()
    con.execute(f"COPY (SELECT * FROM range(100) t(n)) TO '{p}' (FORMAT parquet)")
    con.close()
    (tmp_path / "data2" / "neighbour.csv").write_text("secret\n1\n", encoding="utf-8")
    (tmp_path / "outside" / "secret.csv").write_text("secret\ntop\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def box(world: Path) -> Iterator[Sandbox]:
    yield Sandbox([str(world / "data")], max_rows=50, timeout=10)


def make_junction(link: Path, target: Path) -> bool:
    """Directory link that needs no privileges on Windows; False if it cannot be made."""
    try:
        if os.name == "nt":
            import subprocess

            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
            return r.returncode == 0
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        return False
