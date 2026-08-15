#!/usr/bin/env bash
# Smoke test for the red-proof gate.
#
# Covers every deny path and the full happy path. Creates throwaway git
# repositories under a temporary directory and removes them, along with
# their state files, afterwards. It appends one line to
# ~/.claude/red-proof/exemptions.log, which is by design: exemptions are
# always logged.
#
# Usage: test/smoke_test.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RP_PY="$REPO_ROOT/bin/red_proof.py"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
# Not under /tmp or /var/folders: those are on the gate's allowlist, which
# would make every deny check pass vacuously.
TMP_ROOT="$(mktemp -d "$HOME/.red-proof-smoke.XXXXXX")"
FAILED=0

rp() { python3 "$RP_PY" "$@"; }

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

check_deny() {  # name, hook mode, json payload
  if printf '%s' "$3" | python3 "$RP_PY" hook "$2" | grep -q '"deny"'; then
    pass "$1"
  else
    fail "$1 (expected a deny decision)"
  fi
}

check_allow() {  # name, hook mode, json payload
  local out
  out="$(printf '%s' "$3" | python3 "$RP_PY" hook "$2")"
  if [ -z "$out" ]; then
    pass "$1"
  else
    fail "$1 (expected no output, got: $out)"
  fi
}

new_repo() {  # dir name -> echoes path
  local d="$TMP_ROOT/$1"
  mkdir -p "$d"
  git -C "$d" init -q
  git -C "$d" config user.email smoke@example.invalid
  git -C "$d" config user.name "Smoke Test"
  printf 'def add(a, b):\n    return a + b\n' > "$d/core.py"
  printf 'contract: introduce newmod.x\n' > "$d/contract.md"
  git -C "$d" add -A
  git -C "$d" commit -qm "initial"
  echo "$d"
}

cleanup() {
  for d in "$TMP_ROOT"/*; do
    [ -d "$d" ] || continue
    python3 - "$d" "$CLAUDE_DIR" <<'PY'
import hashlib, os, sys
root, claude_dir = os.path.realpath(sys.argv[1]), sys.argv[2]
key = hashlib.sha256(root.encode()).hexdigest()[:16]
for suffix in (".json", ".contract.md"):
    p = os.path.join(claude_dir, "red-proof", "state", key + suffix)
    if os.path.exists(p):
        os.remove(p)
PY
  done
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

echo "red-proof smoke test"
echo "gate CLI: $RP_PY"
echo

A="$(new_repo main)"
cd "$A" || exit 1

check_deny "production edit is denied with no active cycle" edit \
  "{\"tool_input\":{\"file_path\":\"$A/core.py\"}}"
check_deny "git commit is denied with no passed gate" bash \
  "{\"tool_input\":{\"command\":\"git commit -m x\"},\"cwd\":\"$A\"}"
check_allow "a test file is editable without a cycle" edit \
  "{\"tool_input\":{\"file_path\":\"$A/test_newmod.py\"}}"
check_allow "an unrelated bash command is untouched" bash \
  "{\"tool_input\":{\"command\":\"ls -la\"},\"cwd\":\"$A\"}"
check_allow "quoted 'git commit' is not a false positive" bash \
  "{\"tool_input\":{\"command\":\"echo 'git commit'\"},\"cwd\":\"$A\"}"

rp contract --file contract.md >/dev/null || fail "contract registration"
check_deny "production edit is denied before the freeze" edit \
  "{\"tool_input\":{\"file_path\":\"$A/core.py\"}}"

if rp red --test test_newmod --type contract \
     --expected "ModuleNotFoundError: newmod" \
     -- python3 -c "import newmod" >/dev/null; then
  pass "contract red is recorded for a missing symbol"
else
  fail "contract red"
fi

if rp red --test bogus --type behavior --expected "should not be recorded" \
     -- python3 -c "pass" >/dev/null 2>&1; then
  fail "a command exiting 0 must not be accepted as red"
else
  pass "a command exiting 0 is refused as red"
fi

printf 'import newmod\n\n\ndef test_x():\n    assert newmod.x == 1\n' > test_newmod.py
git add test_newmod.py
rp freeze >/dev/null || fail "freeze"

check_allow "production edit is allowed once tests are frozen" edit \
  "{\"tool_input\":{\"file_path\":\"$A/core.py\"}}"

printf 'x = 1\n' > newmod.py
rp check freeze >/dev/null || fail "freeze check on an untouched frozen test"

printf 'import newmod\n\n\ndef test_x():\n    assert True\n' > test_newmod.py
if rp check freeze >/dev/null 2>&1; then
  fail "a weakened frozen test must be detected"
else
  pass "a weakened frozen test is detected"
fi
git checkout -- test_newmod.py

rp check targeted -- python3 -c "import newmod; assert newmod.x == 1" >/dev/null \
  || fail "targeted check"
rp check full-suite -- python3 -c "import newmod" >/dev/null || fail "full suite check"
rp attest --diff-reviewed --contract-ok >/dev/null || fail "attestation"
rp commit-gate >/dev/null || fail "commit gate"

check_allow "git commit is allowed after the gate passes" bash \
  "{\"tool_input\":{\"command\":\"git add -A && git commit -m feat\"},\"cwd\":\"$A\"}"
git add -A && git commit -qm "feat: add newmod"

check_deny "a second commit on the same gate is denied" bash \
  "{\"tool_input\":{\"command\":\"git commit -m again\"},\"cwd\":\"$A\"}"
check_deny "production edit is denied after the cycle closed" edit \
  "{\"tool_input\":{\"file_path\":\"$A/core.py\"}}"

# Evidence must not survive a code change made after verification.
rp contract --file contract.md >/dev/null
rp red --test test_mod2 --type contract --expected "ModuleNotFoundError: mod2" \
  -- python3 -c "import mod2" >/dev/null
printf 'import mod2\n' > test_mod2.py
git add test_mod2.py && rp freeze >/dev/null
printf 'y = 2\n' > mod2.py
rp check targeted -- python3 -c "import mod2" >/dev/null
rp check full-suite -- python3 -c "import mod2" >/dev/null
rp attest --diff-reviewed --contract-ok >/dev/null
rp commit-gate >/dev/null
printf 'y = 3\n' > mod2.py
if printf '%s' "{\"tool_input\":{\"command\":\"git commit -m stale\"},\"cwd\":\"$A\"}" \
     | python3 "$RP_PY" hook bash | grep -q "stale"; then
  pass "evidence is invalidated by a code change after the gate"
else
  fail "stale evidence was not detected"
fi

B="$(new_repo exempt)"
cd "$B" || exit 1
rp exempt --reason "smoke test" --hours 1 >/dev/null || fail "exempt"
check_allow "a classified exemption allows the commit" bash \
  "{\"tool_input\":{\"command\":\"git commit -m z\"},\"cwd\":\"$B\"}"
check_allow "a classified exemption allows production edits" edit \
  "{\"tool_input\":{\"file_path\":\"$B/core.py\"}}"

# The gate must judge the repository the commit actually runs in, not the
# reported working directory.
C="$(new_repo uncovered)"
check_allow "a cd target repository is resolved, not the session cwd" bash \
  "{\"tool_input\":{\"command\":\"cd $B && git commit -m x\"},\"cwd\":\"$C\"}"
check_deny "a commit into another repository is not covered by this gate" bash \
  "{\"tool_input\":{\"command\":\"cd $C && git commit -m x\"},\"cwd\":\"$B\"}"
check_allow "an unusable directory hint falls back to the reported cwd" bash \
  "{\"tool_input\":{\"command\":\"cd $TMP_ROOT/does-not-exist && git commit -m x\"},\"cwd\":\"$B\"}"
check_allow "git -C on the commit itself is honored" bash \
  "{\"tool_input\":{\"command\":\"git -C $B commit -m x\"},\"cwd\":\"$C\"}"
check_deny "git -C on another subcommand is not the commit target" bash \
  "{\"tool_input\":{\"command\":\"git -C $B status && git commit -m x\"},\"cwd\":\"$C\"}"

echo
if [ "$FAILED" -eq 0 ]; then
  echo "all checks passed"
  exit 0
fi
echo "$FAILED check(s) failed"
exit 1
