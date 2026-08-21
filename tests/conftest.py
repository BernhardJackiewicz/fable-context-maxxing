"""Shared fixtures and helpers for the gate CLI test suite.

Two isolation rules are encoded here:

* Every run gets its own CLAUDE_CONFIG_DIR and its own HOME, so no test
  reads or writes the developer's real state under ~/.claude.
* Repositories used for deny-path tests live under the REAL home. The
  hook allowlists /tmp, /private/tmp, /var/folders and <home>/.claude,
  so a repository under pytest's tmp_path would make every deny check
  pass vacuously.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RP_PY = str(PROJECT_ROOT / "bin" / "red_proof.py")

INITIAL_FILES = {
    "core.py": "def add(a, b):\n    return a + b\n",
    "contract.md": "contract: introduce newmod.x\n",
}
ACCEPTANCE_TEST_BODY = (
    "import newmod\n\n\ndef test_x():\n    assert newmod.x == 1\n"
)


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True)
    assert r.returncode == 0, "git %s failed: %s" % (" ".join(args), r.stderr)
    return r.stdout


@pytest.fixture
def claude_home(tmp_path):
    config = tmp_path / "claude-config"
    home = tmp_path / "fake-home"
    config.mkdir()
    home.mkdir()
    return {"CLAUDE_CONFIG_DIR": str(config), "HOME": str(home)}


@pytest.fixture
def home_dir():
    base = Path(tempfile.mkdtemp(prefix=".rp-lab-tests-", dir=Path.home()))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def git_repo(home_dir):
    def make(name="repo", files=None):
        d = home_dir / name
        d.mkdir(parents=True)
        git(d, "init", "-q")
        git(d, "config", "user.email", "lab@example.invalid")
        git(d, "config", "user.name", "Lab")
        payload = dict(INITIAL_FILES)
        payload.update(files or {})
        for rel, text in payload.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        git(d, "add", "-A")
        git(d, "commit", "-qm", "initial")
        return d
    return make


@pytest.fixture
def rp(claude_home):
    def run(args, env=None, cwd=None, stdin=None):
        child = os.environ.copy()
        child.pop("CLAUDE_CONFIG_DIR", None)
        child.update(claude_home if env is None else env)
        return subprocess.run(
            [sys.executable, RP_PY, *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=child,
            cwd=None if cwd is None else str(cwd),
        )
    return run


def state_file(env, repo):
    key = hashlib.sha256(
        os.path.realpath(str(repo)).encode()).hexdigest()[:16]
    return Path(env["CLAUDE_CONFIG_DIR"]) / "red-proof" / "state" / (key + ".json")


def read_state(env, repo):
    p = state_file(env, repo)
    return json.loads(p.read_text()) if p.exists() else {}


def write_state(env, repo, state):
    p = state_file(env, repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload.setdefault("repo", os.path.realpath(str(repo)))
    p.write_text(json.dumps(payload, indent=2))
    return p


def freeze_cycle(rp, env, repo, test_name="test_newmod.py", contract_args=()):
    """Drive a repository from no state to phase TESTS_FROZEN.

    contract_args are appended to the contract registration, so a test can
    open the cycle with options (an attempt budget, extra required
    evidence) without repeating the whole drive.
    """
    r = rp(["contract", "--file", "contract.md", *contract_args],
           env=env, cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    r = rp(["red", "--test", "test_newmod", "--type", "contract",
            "--expected", "ModuleNotFoundError: newmod",
            "--", sys.executable, "-c", "import newmod"], env=env, cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    (repo / test_name).write_text(ACCEPTANCE_TEST_BODY)
    git(repo, "add", test_name)
    r = rp(["freeze"], env=env, cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    return repo
