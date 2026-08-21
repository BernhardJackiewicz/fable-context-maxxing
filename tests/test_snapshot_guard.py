"""Tests for the worktree snapshot guard beyond the frozen C2 criteria.

The frozen acceptance tests cover the guard on an ordinary failing
command. What is checked here is the surface around it: the threshold
path, the fingerprint the guard actually reads, the escalation message
once the budget is spent, and the budget option itself.
"""

import sys

import pytest

from conftest import freeze_cycle, read_state, write_state

# Touches a marker in the worktree, then fails: the marker proves whether
# the command ran, the exit code drives the fail path.
FAIL_WITH_MARKER = (
    "import pathlib, sys;"
    "pathlib.Path('ran.marker').touch();"
    "sys.exit(1)"
)
PRINT_TOTAL_60 = "print('TOTAL 100 40 60%')"


@pytest.fixture
def frozen(rp, claude_home, git_repo):
    return freeze_cycle(rp, claude_home, git_repo())


def py(code):
    return ["--", sys.executable, "-c", code]


def attempts(claude_home, repo, name):
    return read_state(claude_home, repo).get("gate_attempts", {}).get(name)


def test_a_threshold_violation_counts_as_a_failed_attempt(
        rp, claude_home, frozen):
    r = rp(["check", "coverage", "--min", "85"] + py(PRINT_TOTAL_60),
           cwd=frozen)

    assert r.returncode == 1
    assert attempts(claude_home, frozen, "coverage")["count"] == 1

    r = rp(["check", "coverage", "--min", "85"] + py(PRINT_TOTAL_60),
           cwd=frozen)

    assert r.returncode == 1
    assert "unchanged" in r.stdout
    assert attempts(claude_home, frozen, "coverage")["count"] == 2


def test_a_command_that_writes_into_the_worktree_is_still_a_repeat(
        rp, claude_home, frozen):
    # The run leaves a log behind, so the tree after the failure is not the
    # tree it started from. Neither is a change the developer made, and a
    # rerun must still be refused.
    cmd = py("open('run.log', 'a').write('x'); raise SystemExit(1)")
    assert rp(["check", "targeted"] + cmd, cwd=frozen).returncode == 1
    written = (frozen / "run.log").read_text()

    r = rp(["check", "targeted"] + cmd, cwd=frozen)

    assert r.returncode == 1
    assert "unchanged" in r.stdout
    assert (frozen / "run.log").read_text() == written
    assert attempts(claude_home, frozen, "targeted")["count"] == 2


def test_the_guard_reads_the_full_fingerprint_not_the_production_one(
        rp, claude_home, frozen):
    # "mutation" is the check whose evidence survives non-production
    # changes. The guard must not inherit that: a test-only edit is a
    # repair attempt and has to re-arm the check.
    assert rp(["check", "mutation"] + py(FAIL_WITH_MARKER),
              cwd=frozen).returncode == 1
    (frozen / "ran.marker").unlink()

    r = rp(["check", "mutation"] + py(FAIL_WITH_MARKER), cwd=frozen)

    assert r.returncode == 1
    assert not (frozen / "ran.marker").exists()
    assert "unchanged" in r.stdout

    (frozen / "test_extra.py").write_text("def test_e():\n    assert True\n")
    r = rp(["check", "mutation"] + py(FAIL_WITH_MARKER), cwd=frozen)

    assert r.returncode == 1
    assert (frozen / "ran.marker").exists(), "a test edit must re-arm the guard"
    assert "unchanged" not in r.stdout
    assert attempts(claude_home, frozen, "mutation")["count"] == 3


OTHER_MARKER = (
    "import pathlib, sys;"
    "pathlib.Path('other.marker').touch();"
    "sys.exit(1)"
)


def test_a_corrected_command_runs_and_keeps_the_counter(
        rp, claude_home, frozen):
    # The guard identifies a repeat by tree and command together: a typo
    # in the command is repaired without touching a file, so a tree
    # comparison alone would block the very rerun that fixes it. The
    # budget belongs to the check, not to one spelling of its command, so
    # the corrected run counts on from the previous failure.
    assert rp(["check", "targeted"] + py(FAIL_WITH_MARKER),
              cwd=frozen).returncode == 1
    (frozen / "ran.marker").unlink()
    assert attempts(claude_home, frozen, "targeted")["count"] == 1

    r = rp(["check", "targeted"] + py(OTHER_MARKER), cwd=frozen)

    assert r.returncode == 1
    assert (frozen / "other.marker").exists(), "a corrected command must run"
    assert "unchanged" not in r.stdout
    entry = attempts(claude_home, frozen, "targeted")
    assert entry["count"] == 2
    assert entry["command"].endswith(OTHER_MARKER)


def test_an_attempt_recorded_without_a_command_still_blocks(
        rp, claude_home, frozen):
    # An entry written by an older gate version carries no "command" key.
    # It is read as a match, the conservative direction: the guard keeps
    # blocking where it blocked before instead of waving a rerun through
    # on the strength of a field that was never written.
    assert rp(["check", "targeted"] + py(FAIL_WITH_MARKER),
              cwd=frozen).returncode == 1
    (frozen / "ran.marker").unlink()
    state = read_state(claude_home, frozen)
    del state["gate_attempts"]["targeted"]["command"]
    write_state(claude_home, frozen, state)

    r = rp(["check", "targeted"] + py(OTHER_MARKER), cwd=frozen)

    assert r.returncode == 1
    assert "unchanged" in r.stdout
    assert not (frozen / "other.marker").exists()
    assert attempts(claude_home, frozen, "targeted")["count"] == 2


def test_the_escalation_hint_stays_once_the_budget_is_spent(
        rp, claude_home, git_repo):
    repo = freeze_cycle(rp, claude_home, git_repo(),
                        contract_args=["--max-attempts", "1"])
    assert read_state(claude_home, repo)["max_attempts"] == 1

    r = rp(["check", "static"] + py("raise SystemExit(1)"), cwd=repo)

    assert r.returncode == 1
    assert "attempt 1 of 1" in r.stdout
    assert "re-plan" in r.stdout

    r = rp(["check", "static"] + py("raise SystemExit(1)"), cwd=repo)

    assert r.returncode == 1
    assert "attempt 2 of 1" in r.stdout
    assert "re-plan" in r.stdout


@pytest.mark.parametrize("value", ["0", "-3", "many"])
def test_an_unusable_attempt_budget_is_a_usage_error(rp, git_repo, value):
    repo = git_repo()

    r = rp(["contract", "--file", "contract.md", "--max-attempts", value],
           cwd=repo)

    assert r.returncode == 1
    assert "--max-attempts" in r.stdout
