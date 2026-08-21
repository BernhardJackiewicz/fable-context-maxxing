"""Subprocess characterization tests for the PreToolUse hook modes.

The hook resolves state through the base directory, so every call runs as
a subprocess with an isolated CLAUDE_CONFIG_DIR (see conftest.py).
"""

import json

import pytest

from conftest import freeze_cycle, write_state


def edit_payload(path):
    return json.dumps({"tool_input": {"file_path": str(path)}})


def bash_payload(command, cwd):
    return json.dumps({"tool_input": {"command": command}, "cwd": str(cwd)})


def assert_allow(result):
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout


def assert_deny(result, needle):
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert needle in decision["permissionDecisionReason"]


def touch(repo, rel):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x = 1\n")
    return p


# --- hook edit: allow paths -----------------------------------------------

@pytest.mark.parametrize("payload", [
    "{}",
    json.dumps({"tool_input": {}}),
    json.dumps({"tool_input": {"file_path": ""}}),
])
def test_edit_without_a_file_path_is_allowed(rp, payload):
    assert_allow(rp(["hook", "edit"], stdin=payload))


@pytest.mark.parametrize("path", [
    "/tmp/lab-prod.py",
    "/private/tmp/lab-prod.py",
    "/var/folders/lab-prod.py",
])
def test_edit_under_an_allowlisted_prefix_is_allowed(rp, path):
    assert_allow(rp(["hook", "edit"], stdin=edit_payload(path)))


def test_edit_outside_a_git_repository_is_allowed(rp, home_dir):
    loose = home_dir / "loose.py"
    loose.write_text("x = 1\n")
    assert_allow(rp(["hook", "edit"], stdin=edit_payload(loose)))


@pytest.mark.parametrize("rel", [
    "tests/helper.py",
    "test/helper.py",
    "spec/helper.py",
    "docs/helper.py",
    "doc/helper.py",
    "examples/helper.py",
    ".claude/helper.py",
    ".red-proof/helper.py",
    "scratchpad/helper.py",
])
def test_edit_of_a_non_production_directory_is_allowed(rp, git_repo, rel):
    repo = git_repo()
    target = touch(repo, rel)
    # Control: without the marker the same repository denies, so a pass
    # here cannot come from a missing cycle or an unresolved repository.
    assert_deny(rp(["hook", "edit"], stdin=edit_payload(repo / "core.py")),
                "no active cycle")
    assert_allow(rp(["hook", "edit"], stdin=edit_payload(target)))


@pytest.mark.parametrize("rel", [
    "test_thing.py",
    "conftest.py",
    "NOTES.md",
    "guide.rst",
    "notes.txt",
    "thing_test.py",
    "thing_test.go",
    "thing.spec.ts",
    "thing.test.tsx",
])
def test_edit_of_a_non_production_filename_is_allowed(rp, git_repo, rel):
    repo = git_repo()
    assert_allow(rp(["hook", "edit"], stdin=edit_payload(touch(repo, rel))))


def test_edit_under_an_active_exemption_is_allowed(rp, git_repo):
    repo = git_repo()
    r = rp(["exempt", "--reason", "docs only"], cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert_allow(rp(["hook", "edit"], stdin=edit_payload(repo / "core.py")))


def test_edit_is_allowed_once_the_tests_are_frozen(rp, claude_home, git_repo):
    repo = freeze_cycle(rp, claude_home, git_repo())
    assert_allow(rp(["hook", "edit"], stdin=edit_payload(repo / "core.py")))


# --- hook edit: deny paths ------------------------------------------------

def test_edit_without_a_cycle_is_denied(rp, git_repo):
    repo = git_repo()
    assert_deny(rp(["hook", "edit"], stdin=edit_payload(repo / "core.py")),
                "no active cycle")


def test_edit_before_the_freeze_is_denied(rp, git_repo):
    repo = git_repo()
    r = rp(["contract", "--file", "contract.md"], cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert_deny(rp(["hook", "edit"], stdin=edit_payload(repo / "core.py")),
                "acceptance tests are not frozen yet")


def test_edit_after_the_cycle_closed_is_denied(rp, claude_home, git_repo):
    repo = git_repo()
    write_state(claude_home, repo, {"phase": "COMMIT_ISSUED"})
    assert_deny(rp(["hook", "edit"], stdin=edit_payload(repo / "core.py")),
                "previous commit cycle is closed")


# --- hook bash ------------------------------------------------------------

def test_commit_without_a_passed_gate_is_denied(rp, git_repo):
    repo = git_repo()
    assert_deny(rp(["hook", "bash"],
                   stdin=bash_payload("git commit -m x", repo)),
                "Commit Gate has not passed")


@pytest.mark.parametrize("command", [
    "echo 'git commit'",
    'echo "git commit"',
    "ls -la",
    "git status",
])
def test_a_command_that_is_not_a_commit_is_allowed(rp, git_repo, command):
    repo = git_repo()
    assert_allow(rp(["hook", "bash"], stdin=bash_payload(command, repo)))


def test_commit_outside_a_git_repository_is_allowed(rp, home_dir):
    assert_allow(rp(["hook", "bash"],
                    stdin=bash_payload("git commit -m x", home_dir)))


def test_commit_under_an_active_exemption_is_allowed(rp, claude_home, git_repo):
    repo = git_repo()
    r = rp(["exempt", "--reason", "docs only"], cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert_allow(rp(["hook", "bash"],
                    stdin=bash_payload("git commit -m x", repo)))
    log = (claude_home["CLAUDE_CONFIG_DIR"] + "/red-proof/exemptions.log")
    with open(log) as f:
        assert "commit under exemption" in f.read()
