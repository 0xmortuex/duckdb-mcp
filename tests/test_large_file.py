"""A million-row file: queries must stream, cap their output and stay fast."""

from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb
import pytest

from duckdb_mcp import server
from duckdb_mcp.sandbox import Sandbox

ROWS = 1_000_000


@pytest.fixture(scope="module")
def big(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("big")
    con = duckdb.connect()
    src = "SELECT i AS id, 'user_' || (i % 1000) AS name, i % 7 AS bucket, random() AS score FROM range(1000000) t(i)"
    con.execute(f"COPY ({src}) TO '{(d / 'big.parquet').as_posix()}' (FORMAT parquet)")
    con.execute(f"COPY ({src}) TO '{(d / 'big.csv').as_posix()}' (FORMAT csv, HEADER)")
    con.close()
    return d


@pytest.mark.parametrize("name", ["big.csv", "big.parquet"])
def test_select_star_streams_and_caps(big: Path, name: str) -> None:
    box = Sandbox([str(big)], max_rows=1000, timeout=60)
    start = time.monotonic()
    res = box.run(f"SELECT * FROM '{name}'")
    elapsed = time.monotonic() - start
    assert len(res.rows) == 1000 and res.truncated
    # Pulling a million rows into Python takes many seconds; streaming the first
    # thousand is near instant.
    assert elapsed < 5, f"took {elapsed:.1f}s - not streaming?"


@pytest.mark.parametrize("name", ["big.csv", "big.parquet"])
def test_aggregates_see_every_row(big: Path, name: str) -> None:
    box = Sandbox([str(big)], max_rows=1000, timeout=60)
    assert box.run(f"SELECT count(*), max(id) FROM '{name}'").rows == [(ROWS, ROWS - 1)]
    groups = box.run(f"SELECT bucket, count(*) FROM '{name}' GROUP BY 1 ORDER BY 1")
    assert sum(r[1] for r in groups.rows) == ROWS and len(groups.rows) == 7


def test_truncation_notice_on_a_big_result(big: Path) -> None:
    server.configure([str(big)], max_rows=1000, timeout=60)
    try:
        out = server.query("SELECT * FROM 'big.parquet'")
        assert "TRUNCATED: showing the first 1000 rows" in out
        # the whole answer stays small enough for an LLM context
        assert len(out) < 100_000
        data = json.loads(server.query("SELECT * FROM 'big.csv'", format="json"))
        assert data["row_count"] == 1000 and data["truncated"]
    finally:
        server._box = None


def test_describe_and_profile_big_file(big: Path) -> None:
    server.configure([str(big)], max_rows=1000, timeout=60)
    try:
        out = server.describe_file("big.csv")
        assert "Rows: 1,000,000" in out
        prof = server.profile_file("big.parquet")
        assert "Rows: 1,000,000" in prof
    finally:
        server._box = None



CHILD = """
import psutil
from duckdb_mcp.sandbox import Sandbox

box = Sandbox([{folder!r}], max_rows=1000, timeout=60)
res = box.run("SELECT * FROM 'big.csv'")
assert len(res.rows) == 1000 and res.truncated
mi = psutil.Process().memory_info()
peak = getattr(mi, "peak_wset", None)
if peak is None:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
print(peak // (1024 * 1024))
"""


def test_peak_memory_stays_small_when_streaming(big: Path) -> None:
    """Run the capped SELECT * in a fresh process and check its peak memory."""
    import subprocess
    import sys

    code = CHILD.format(folder=str(big))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    peak_mb = int(r.stdout.strip().splitlines()[-1])
    csv_mb = (big / "big.csv").stat().st_size // (1024 * 1024)
    # Materialising a million rows as Python tuples would need far more than this.
    assert peak_mb < 600, f"peak {peak_mb} MB for a {csv_mb} MB csv"
    print(f"peak working set {peak_mb} MB while capping a {csv_mb} MB csv")
