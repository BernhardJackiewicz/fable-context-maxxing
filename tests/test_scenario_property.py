"""Tests for the two lab-D1 additions: scenario reds and seeded properties.

Three subjects, in that order: extract_hypothesis_seed as a pure function,
the non-production classification of Gherkin files, and the seed
precondition of "check property" as it behaves through the CLI, where the
ordering against the repeat guard is what actually matters.
"""

import json
import sys
from pathlib import Path

import pytest

from conftest import freeze_cycle, read_state

BIN_DIR = str(Path(__file__).resolve().parents[1] / "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import red_proof  # noqa: E402


# --- extract_hypothesis_seed ----------------------------------------------

@pytest.mark.parametrize("command, seed", [
    ("pytest --hypothesis-seed=1234 -q", 1234.0),
    ("env HYPOTHESIS_SEED=1234 pytest -q", 1234.0),
    ("HYPOTHESIS_SEED=1234 pytest -q", 1234.0),
    ("pytest --hypothesis-seed=0", 0.0),          # zero is a seed, not "unset"
    ("pytest --hypothesis-seed=12.5", 12.5),      # a decimal keeps its fraction
    ("env HYPOTHESIS_SEED=0.5 pytest", 0.5),
    ("sh -c 'HYPOTHESIS_SEED=42 pytest'", 42.0),  # quoted, as a shell string
])
def test_a_seed_token_is_read_from_the_command(command, seed):
    assert red_proof.extract_hypothesis_seed(command) == {
        "hypothesis_seed": seed}


@pytest.mark.parametrize("command, seed", [
    ("pytest --hypothesis-seed=1 --hypothesis-seed=2", 2.0),
    ("env HYPOTHESIS_SEED=1 HYPOTHESIS_SEED=2 pytest", 2.0),
    ("env HYPOTHESIS_SEED=7 pytest --hypothesis-seed=9", 9.0),
    ("pytest --hypothesis-seed=9 && env HYPOTHESIS_SEED=7 pytest", 7.0),
])
def test_the_last_seed_in_the_command_wins(command, seed):
    # Documented rule: when a command names several seeds, the last one
    # wins, whichever spelling it uses. That is what a shell does with a
    # repeated assignment and what an argument parser does with a repeated
    # flag, so the recorded number is the one the run actually used.
    assert red_proof.extract_hypothesis_seed(command) == {
        "hypothesis_seed": seed}


@pytest.mark.parametrize("command", [
    None,
    "",
    "pytest -q tests/",
    "pytest --hypothesis-seed",              # a flag without a value
    "pytest --hypothesis-seed=random",       # not a number
    "pytest --hypothesis-seeds=3",           # a different flag
    "env MY_HYPOTHESIS_SEED=3 pytest",       # glued to a longer name
    "env HYPOTHESIS_SEED_FILE=x pytest",     # a different variable
])
def test_a_command_without_a_seed_yields_no_metric(command):
    assert red_proof.extract_hypothesis_seed(command) is None


def test_the_property_entry_reads_the_command_not_the_output():
    spec = red_proof.CHECKS["property"]
    assert spec["evidence_key"] == "property"
    assert spec["staleness"] == "strict"
    assert spec["extract"] is red_proof.extract_hypothesis_seed
    assert spec["extract_from"] == "command"


# --- Gherkin files --------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "login.feature",
    "features/login.feature",
    "features/auth/login.feature",
    "src/app/features/checkout/pay.feature",
    "./features/login.feature",
    "features\\auth\\login.feature",
])
def test_a_feature_file_in_any_directory_is_non_production(rel):
    assert red_proof.is_nonprod(rel)


def test_the_edit_hook_allows_a_feature_file_in_a_subdirectory(rp, git_repo):
    repo = git_repo()
    steps = repo / "features" / "auth"
    steps.mkdir(parents=True)
    (steps / "login.feature").write_text("Feature: login\n")
    (steps / "steps.py").write_text("x = 1\n")

    def hook(name):
        payload = json.dumps({"tool_input": {"file_path": str(steps / name)}})
        return rp(["hook", "edit"], cwd=repo, stdin=payload)

    allowed = hook("login.feature")
    assert allowed.returncode == 0
    assert allowed.stdout.strip() == ""
    # Same directory, same repository without a cycle: the suffix is what
    # decides, so the step implementation next to it is still denied.
    assert '"deny"' in hook("steps.py").stdout


# --- scenario reds --------------------------------------------------------

# A failing run as a Gherkin runner prints it, simulated so the test needs
# no pytest-bdd installation: the gate only ever sees exit code and output.
BDD_RUNNER = '''\
import sys

print("features/login.feature:2: Scenario: a user signs in")
print("StepDefinitionNotFoundError: Step definition is not found: "
      'Given "a registered user"')
print("1 failed in 0.01s")
sys.exit(1)
'''

FEATURE = ("Feature: login\n"
           "  Scenario: a user signs in\n"
           "    Given a registered user\n")


@pytest.fixture
def bdd_repo(git_repo):
    repo = git_repo()
    (repo / "features").mkdir()
    (repo / "features" / "login.feature").write_text(FEATURE)
    (repo / "bdd_runner.py").write_text(BDD_RUNNER)
    return repo


def test_a_scenario_red_records_a_failing_gherkin_run(rp, claude_home, bdd_repo):
    assert rp(["contract", "--file", "contract.md"],
              cwd=bdd_repo).returncode == 0

    r = rp(["red", "--test", "features/login.feature", "--type", "scenario",
            "--expected", "step definition is not found",
            "--", sys.executable, str(bdd_repo / "bdd_runner.py")],
           cwd=bdd_repo)

    assert r.returncode == 0, r.stdout + r.stderr
    state = read_state(claude_home, bdd_repo)
    assert state["phase"] == "RED_CONFIRMED"
    proof = state["red_proofs"][0]
    assert proof["red_type"] == "scenario"
    assert proof["test"] == "features/login.feature"
    assert proof["exit_code"] == 1
    assert "StepDefinitionNotFoundError" in proof["actual_output_tail"]


def test_a_passing_scenario_command_is_refused_as_a_red(
        rp, claude_home, bdd_repo):
    assert rp(["contract", "--file", "contract.md"],
              cwd=bdd_repo).returncode == 0

    r = rp(["red", "--test", "features/login.feature", "--type", "scenario",
            "--expected", "steps are green already",
            "--", sys.executable, "-c", "print('1 passed in 0.01s')"],
           cwd=bdd_repo)

    assert r.returncode == 1
    assert read_state(claude_home, bdd_repo)["red_proofs"] == []


def test_the_usage_line_names_every_red_type(rp, git_repo):
    r = rp(["red", "--test", "t", "--type", "wild", "--expected", "x",
            "--", sys.executable, "-c", "raise SystemExit(1)"],
           cwd=git_repo())

    assert r.returncode == 1
    for kind in red_proof.RED_TYPES:
        assert kind in r.stdout


# --- check property through the CLI ---------------------------------------

@pytest.fixture
def frozen(rp, claude_home, git_repo):
    return freeze_cycle(rp, claude_home, git_repo(),
                        contract_args=("--require", "property"))


def marker_cmd(repo, *prefix):
    """A command that leaves a file behind, so "it never ran" is provable."""
    return [*prefix, sys.executable, "-c",
            "open(%r, 'w').close()" % str(repo / "ran.txt")]


def test_a_missing_seed_neither_runs_the_command_nor_costs_an_attempt(
        rp, claude_home, frozen):
    r = rp(["check", "property", "--", *marker_cmd(frozen)], cwd=frozen)

    assert r.returncode == 1
    assert "seed" in r.stdout.lower()
    assert not (frozen / "ran.txt").exists()
    state = read_state(claude_home, frozen)
    assert "property" not in state["evidence"]
    # A usage error is not a failed attempt: the budget is untouched and
    # the repeat guard is not armed, so adding the seed works right away,
    # on the very same worktree.
    assert state["gate_attempts"] == {}

    r = rp(["check", "property", "--",
            *marker_cmd(frozen, "env", "HYPOTHESIS_SEED=42")], cwd=frozen)

    assert r.returncode == 0, r.stdout + r.stderr
    assert (frozen / "ran.txt").exists()
    ev = read_state(claude_home, frozen)["evidence"]["property"]
    assert ev["metrics"] == {"hypothesis_seed": 42.0}


def test_a_seed_flag_is_recorded_with_the_command(rp, claude_home, frozen):
    r = rp(["check", "property", "--", sys.executable, "-c", "print('ok')",
            "--hypothesis-seed=1234"], cwd=frozen)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "hypothesis_seed=1234" in r.stdout       # reported to the reader
    ev = read_state(claude_home, frozen)["evidence"]["property"]
    assert ev["metrics"] == {"hypothesis_seed": 1234.0}
    assert ev["command"].endswith("--hypothesis-seed=1234")


def test_the_seed_comes_from_the_command_even_when_the_output_disagrees(
        rp, claude_home, frozen):
    # The run prints a seed of its own; the recorded one is the seed the
    # command names, because that is what a reader can repeat.
    r = rp(["check", "property", "--", sys.executable, "-c",
            "print('HYPOTHESIS_SEED=999')", "--hypothesis-seed=11"],
           cwd=frozen)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "HYPOTHESIS_SEED=999" in r.stdout
    ev = read_state(claude_home, frozen)["evidence"]["property"]
    assert ev["metrics"] == {"hypothesis_seed": 11.0}


def test_property_evidence_is_strict_about_a_test_edit(
        rp, claude_home, frozen):
    # Unlike the mutation gate, property evidence is not worth keeping
    # across a test edit: a property run is cheap and the seed says nothing
    # about the tree it ran on.
    assert red_proof.staleness_policy("property") == "strict"
    r = rp(["check", "property", "--", "env", "HYPOTHESIS_SEED=5",
            sys.executable, "-c", "pass"], cwd=frozen)
    assert r.returncode == 0, r.stdout + r.stderr

    (frozen / "test_extra.py").write_text("def test_extra():\n    assert True\n")

    r = rp(["commit-gate"], cwd=frozen)
    assert r.returncode == 1
    assert "stale evidence (code changed since): property" in r.stdout


@pytest.mark.parametrize("seed, minimum", [("7", "5"), ("3", "5")])
def test_min_is_refused_on_property_whatever_the_seed_is(
        rp, claude_home, frozen, seed, minimum):
    # This replaces the earlier characterization, which recorded that
    # --min compared the seed itself and let a high seed pass a low
    # threshold. A seed is an input value, not a quality of the run, so a
    # threshold on it grades what the caller typed. The refusal is read
    # off the registry (extract_from == "command"), which is why neither
    # the seed nor the threshold changes the outcome, and it is a usage
    # error: nothing runs, no evidence, no attempt spent.
    r = rp(["check", "property", "--min", minimum, "--",
            *marker_cmd(frozen, "env", "HYPOTHESIS_SEED=" + seed)], cwd=frozen)

    assert r.returncode == 1
    assert "--min" in r.stdout
    assert not (frozen / "ran.txt").exists()
    state = read_state(claude_home, frozen)
    assert "property" not in state["evidence"]
    assert state["gate_attempts"] == {}
