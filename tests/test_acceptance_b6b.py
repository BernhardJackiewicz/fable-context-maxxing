"""Acceptance test for contract lab-B6b: the static gate baseline."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_ruff_clean_on_gate_and_bench_core():
    ruff = shutil.which("ruff") or str(
        Path.home() / "Library/Python/3.9/bin/ruff")
    if not Path(ruff).exists():
        pytest.fail("ruff not available on this machine")
    r = subprocess.run(
        [ruff, "check", "bin/", "bench/bench.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
