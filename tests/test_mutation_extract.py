"""Characterization tests for the mutation gate.

Two subjects, both of them parts of the same gate: extract_mutation as a
pure function, and the ignore list that keeps the artefacts of a mutation
run (the copied "mutants" tree, the cache) out of the fingerprints.

The frozen acceptance file covers the headline cases; what is pinned here
is the behaviour at the edges, which is where a later refactoring of the
extractor would quietly change the recorded number.
"""

import sys
from pathlib import Path

import pytest

BIN_DIR = str(Path(__file__).resolve().parents[1] / "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import red_proof  # noqa: E402


def score(output):
    return red_proof.extract_mutation(output)["mutation_score"]


# --- the score is a measurement, not a display value ----------------------

def test_a_repeating_fraction_is_not_rounded():
    measured = score("Killed 1 out of 3")
    assert measured == pytest.approx(100 / 3, abs=1e-12)
    assert measured != 33.33
    assert measured != 33.0


def test_the_acceptance_ratio_keeps_its_full_precision():
    assert score("Killed 13 out of 15") == pytest.approx(1300 / 15, abs=1e-12)


@pytest.mark.parametrize("text,expected", [
    ("Killed 1 out of 8", 12.5),
    ("Killed 3 out of 8", 37.5),
    ("Killed 1 out of 1", 100.0),
    ("Killed 0 out of 7", 0.0),
    ("7/8  KILLED", 87.5),
])
def test_exact_fractions_are_reproduced_exactly(text, expected):
    assert score(text) == expected


def test_a_large_run_is_read_without_loss():
    assert score("Killed 999999 out of 1000000") == pytest.approx(
        99.9999, abs=1e-9)
    assert score("1234567/2000000  KILLED") == pytest.approx(
        61.72835, abs=1e-9)


# --- which line is read ---------------------------------------------------

def test_the_last_matching_line_wins():
    out = ("Killed 1 out of 10\n"
           "progress noise\n"
           "Killed 9 out of 10\n")
    assert score(out) == 90.0


def test_the_last_line_wins_across_the_two_formats():
    assert score("Killed 1 out of 10\n8/10  KILLED\n") == 80.0
    assert score("8/10  KILLED\nKilled 1 out of 10\n") == 10.0


def test_surrounding_lines_are_ignored():
    out = ("mutmut run\n"
           "=== session starts ===\n"
           "12/15  KILLED\n"
           "survivors written to mutants/\n")
    assert score(out) == 80.0


# --- format variants ------------------------------------------------------

@pytest.mark.parametrize("line", [
    "12/15 KILLED",
    "12/15  KILLED",
    "12/15\tKILLED",
    "12 / 15  KILLED",
    "12/ 15  KILLED",
    "  12/15  KILLED  ",
    "results: 12/15  KILLED",
    "12/15  KILLED (survivors listed below)",
])
def test_killed_rows_are_read_whatever_the_spacing(line):
    assert score(line) == 80.0


@pytest.mark.parametrize("line", [
    "Killed 12 out of 15",
    "  Killed 12 out of 15",
    "Killed  12  out  of  15",
    "mutmut: Killed 12 out of 15 mutants",
])
def test_summary_sentences_are_read_whatever_the_spacing(line):
    assert score(line) == 80.0


# --- what is not a measurement --------------------------------------------

@pytest.mark.parametrize("out", [
    "Killed 0 out of 0",
    "0/0  KILLED",
    "Killed 12 out of 15\nKilled 0 out of 0\n",   # the last line decides
    "12/15  KILLED\n0/0  KILLED\n",
])
def test_an_empty_run_is_no_measurement(out):
    assert red_proof.extract_mutation(out) is None


@pytest.mark.parametrize("out", [
    "",
    None,
    "no mutation talk here\n",
    "5 passed in 0.10s\n",
    "12/15 SURVIVED\n",
    "Killed 12 mutants\n",
    "Killed out of 15\n",
])
def test_output_without_a_result_yields_no_metric(out):
    assert red_proof.extract_mutation(out) is None


def test_a_metric_free_run_fails_a_threshold_instead_of_dividing_by_zero():
    # --min compares metric_value, and None is what makes cmd_check refuse
    # to record evidence rather than claim a score.
    assert red_proof.metric_value(
        red_proof.extract_mutation("Killed 0 out of 0")) is None


# --- the artefacts of a mutation run --------------------------------------

def test_both_artefacts_are_listed_in_the_ignore_set():
    assert ".mutmut-cache" in red_proof.FP_IGNORE
    assert "mutants" in red_proof.FP_IGNORE


@pytest.mark.parametrize("path", [
    "mutants",
    "mutants/m1.py",
    "mutants/pkg/deep/m2.py",
    "pkg/mutants/m3.py",
    ".mutmut-cache",
    ".mutmut-cache-journal",
    "mutants\\m4.py",
])
def test_mutation_artefacts_are_skipped(path):
    assert red_proof.fp_skip(path) is True


@pytest.mark.parametrize("path", [
    "mutants_util.py",
    "src/mutants_util.py",
    "src/my_mutants/core.py",
    "mutants.py",
    "docs/mutants.md",
])
def test_a_name_that_only_contains_the_word_still_counts_as_code(path):
    assert red_proof.fp_skip(path) is False


def test_a_file_named_after_the_artefact_dir_still_moves_the_fingerprint(
        git_repo):
    repo = git_repo()
    before = red_proof.fingerprint(str(repo))

    (repo / "mutants_util.py").write_text("x = 1\n")

    assert red_proof.fingerprint(str(repo)) != before


def test_the_artefacts_themselves_move_no_fingerprint(git_repo):
    repo = git_repo()
    before = red_proof.fingerprint(str(repo))
    prod_before = red_proof.production_fingerprint(str(repo))

    (repo / "mutants" / "pkg").mkdir(parents=True)
    (repo / "mutants" / "pkg" / "core.py").write_text("x = 1\n")
    (repo / ".mutmut-cache").write_text("cache\n")

    assert red_proof.fingerprint(str(repo)) == before
    assert red_proof.production_fingerprint(str(repo)) == prod_before
