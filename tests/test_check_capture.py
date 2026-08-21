"""Characterization tests for output capture, metric extraction and --min.

Two levels: extract_coverage is exercised as a pure function, and the
capture rules (pass-through, tail truncation, the shape of the evidence
entry) are exercised through the CLI, because that is where the tail is
actually assembled.
"""

import sys
from pathlib import Path

import pytest

from conftest import freeze_cycle, read_state

BIN_DIR = str(Path(__file__).resolve().parents[1] / "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import red_proof  # noqa: E402


# --- extract_coverage -----------------------------------------------------

def test_a_plain_total_line_is_read():
    assert red_proof.extract_coverage(
        "TOTAL   120   10   91%\n") == {"coverage_percent": 91.0}


def test_the_last_total_line_wins():
    out = ("Name  Stmts  Miss  Cover\n"
           "TOTAL   100   50   50%\n"
           "\n"
           "Name  Stmts  Miss  Cover\n"
           "TOTAL   200   10   95%\n")
    assert red_proof.extract_coverage(out) == {"coverage_percent": 95.0}


def test_full_coverage_is_not_confused_with_a_missing_value():
    assert red_proof.extract_coverage(
        "TOTAL   42   0   100%\n") == {"coverage_percent": 100.0}


def test_a_decimal_percentage_keeps_its_fraction():
    assert red_proof.extract_coverage(
        "TOTAL   800   17   87.63%\n") == {"coverage_percent": 87.63}


def test_a_branch_coverage_row_uses_the_trailing_percentage():
    assert red_proof.extract_coverage(
        "TOTAL   400   20   120   14   93%\n") == {"coverage_percent": 93.0}


def test_an_indented_total_line_is_still_found():
    assert red_proof.extract_coverage(
        "  TOTAL   10   1   90%\n") == {"coverage_percent": 90.0}


@pytest.mark.parametrize("out", [
    "",
    "no percentages here\n",
    "5 passed in 0.10s\n",
    "src/mod.py   120   10   91%\n",          # a per-file row is not the total
    "TOTAL   120   10\n",                     # a total without a percentage
    "SUBTOTALS   120   10   91%\n",           # TOTAL must start the row
])
def test_output_without_a_total_percentage_yields_no_metric(out):
    assert red_proof.extract_coverage(out) is None


# --- capture through the CLI ----------------------------------------------

@pytest.fixture
def frozen(rp, claude_home, git_repo):
    return freeze_cycle(rp, claude_home, git_repo())


def evidence(claude_home, repo, key):
    return read_state(claude_home, repo).get("evidence", {}).get(key)


def py(code):
    return ["--", sys.executable, "-c", code]


def test_stdout_and_stderr_are_both_passed_through_and_tailed(
        rp, claude_home, frozen):
    r = rp(["check", "targeted"] + py(
        "import sys; print('to-out'); sys.stderr.write('to-err\\n')"),
        cwd=frozen)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "to-out" in r.stdout
    assert "to-err" in r.stderr
    tail = evidence(claude_home, frozen, "targeted")["output_tail"]
    assert "to-out" in tail and "to-err" in tail


def test_the_tail_is_truncated_to_the_last_2000_characters(
        rp, claude_home, frozen):
    r = rp(["check", "targeted"] + py(
        "print('A' * 3000); print('END-MARKER')"), cwd=frozen)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "A" * 3000 in r.stdout          # pass-through stays complete
    tail = evidence(claude_home, frozen, "targeted")["output_tail"]
    assert len(tail) == 2000
    assert tail.endswith("END-MARKER\n")   # the end is what is kept
    assert tail.count("A") < 3000


def test_a_check_without_an_extractor_records_null_metrics_and_min(
        rp, claude_home, frozen):
    assert rp(["check", "targeted"] + py("pass"), cwd=frozen).returncode == 0

    ev = evidence(claude_home, frozen, "targeted")
    assert ev["metrics"] is None and ev["min"] is None
    assert ev["output_tail"] == ""


def test_coverage_without_min_still_records_the_measurement(
        rp, claude_home, frozen):
    r = rp(["check", "coverage"] + py("print('TOTAL 10 1 90%')"), cwd=frozen)

    assert r.returncode == 0, r.stdout + r.stderr
    ev = evidence(claude_home, frozen, "coverage")
    assert ev["metrics"] == {"coverage_percent": 90.0}
    assert ev["min"] is None


def test_coverage_without_a_measurement_and_without_min_is_green(
        rp, claude_home, frozen):
    r = rp(["check", "coverage"] + py("print('nothing to see')"), cwd=frozen)

    assert r.returncode == 0, r.stdout + r.stderr
    assert evidence(claude_home, frozen, "coverage")["metrics"] is None


def test_a_measurement_exactly_at_the_threshold_passes(
        rp, claude_home, frozen):
    r = rp(["check", "coverage", "--min", "85"] + py(
        "print('TOTAL 100 15 85%')"), cwd=frozen)

    assert r.returncode == 0, r.stdout + r.stderr
    assert evidence(claude_home, frozen, "coverage")["min"] == 85.0


@pytest.mark.parametrize("value", ["eighty", "85%", ""])
def test_a_non_numeric_min_is_a_usage_error(rp, claude_home, frozen, value):
    r = rp(["check", "coverage", "--min", value] + py(
        "print('TOTAL 100 1 99%')"), cwd=frozen)

    assert r.returncode == 1
    assert evidence(claude_home, frozen, "coverage") is None


def test_min_without_a_value_is_a_usage_error(rp, claude_home, frozen):
    r = rp(["check", "coverage", "--min"] + py("print('TOTAL 100 1 99%')"),
           cwd=frozen)

    assert r.returncode == 1
    assert "usage: check coverage" in r.stdout
    assert evidence(claude_home, frozen, "coverage") is None


def test_min_on_a_check_without_a_metric_names_the_usable_checks(
        rp, claude_home, frozen):
    r = rp(["check", "static", "--min", "5"] + py("pass"), cwd=frozen)

    assert r.returncode == 1
    assert "coverage" in r.stdout
    assert evidence(claude_home, frozen, "static") is None


def test_a_failing_command_records_nothing_even_with_a_good_metric(
        rp, claude_home, frozen):
    r = rp(["check", "coverage", "--min", "10"] + py(
        "print('TOTAL 100 1 99%'); raise SystemExit(3)"), cwd=frozen)

    assert r.returncode == 1
    assert "TOTAL 100 1 99%" in r.stdout    # still passed through
    assert evidence(claude_home, frozen, "coverage") is None
