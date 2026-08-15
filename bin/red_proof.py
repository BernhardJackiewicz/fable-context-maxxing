#!/usr/bin/env python3
"""red-proof gate: mechanical enforcement for the red-proof methodology.

State machine per repository:
    CONTRACT_CREATED -> RED_CONFIRMED -> TESTS_FROZEN -> COMMIT_ISSUED

Evidence (targeted tests, full suite, attestation, commit gate) is bound
to a content fingerprint: HEAD plus the content of every modified or
untracked file. Staging (git add) does not change the fingerprint;
any real code change does, which invalidates prior evidence.

Hook mode fails open on internal errors: this is process CI for the
agentic workflow, not a security boundary.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time

BASE_DIR = os.path.expanduser("~/.claude/red-proof")
STATE_DIR = os.path.join(BASE_DIR, "state")
EXEMPT_LOG = os.path.join(BASE_DIR, "exemptions.log")
ERROR_LOG = os.path.join(BASE_DIR, "error.log")
DEFAULT_EXEMPT_HOURS = 4.0

FP_IGNORE = ("__pycache__", ".pytest_cache", ".red-proof", "node_modules",
             ".DS_Store", ".coverage", ".mypy_cache", ".ruff_cache",
             ".venv", ".tox", ".egg-info")

NONPROD_MARKERS = ("/tests/", "/test/", "/spec/", "/docs/", "/doc/",
                   "/examples/", "/.claude/", "/.red-proof/", "/scratchpad/")
NONPROD_PREFIXES = ("test_", "conftest")
NONPROD_SUFFIXES = (".md", ".rst", ".txt", "_test.py", "_test.go",
                    ".spec.ts", ".spec.js", ".spec.tsx",
                    ".test.ts", ".test.js", ".test.tsx")

ALLOW_PATH_PREFIXES = (
    os.path.realpath(os.path.expanduser("~/.claude")),
    "/tmp", "/private/tmp", "/var/folders",
)

GIT_COMMIT_RE = re.compile(r"\bgit(\s+(-C\s+\S+|-c\s+\S+))*\s+commit\b")


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def repo_root(path):
    if not path:
        return None
    code, out, _ = run(["git", "-C", path, "rev-parse", "--show-toplevel"])
    if code != 0:
        return None
    return os.path.realpath(out.strip())


def state_path(root):
    key = hashlib.sha256(root.encode()).hexdigest()[:16]
    return os.path.join(STATE_DIR, key + ".json")


def load_state(root):
    try:
        with open(state_path(root)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(root, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    state["repo"] = root
    with open(state_path(root), "w") as f:
        json.dump(state, f, indent=2)


def fp_skip(path):
    return any(part in path for part in FP_IGNORE) or path.endswith(".pyc")


def changed_paths(root):
    code, out, _ = run(
        ["git", "status", "--porcelain=v1", "-z", "-uall"], root)
    if code != 0:
        raise RuntimeError("git status failed in " + root)
    toks = out.split("\0")
    paths, i = [], 0
    while i < len(toks):
        t = toks[i]
        if not t:
            i += 1
            continue
        status, p = t[:2], t[3:]
        paths.append(p)
        if status and status[0] in "RC":
            i += 1
            if i < len(toks) and toks[i]:
                paths.append(toks[i])
        i += 1
    return paths


def fingerprint(root):
    code, out, _ = run(["git", "rev-parse", "HEAD"], root)
    head = out.strip() if code == 0 else "NO_HEAD"
    h = hashlib.sha256(head.encode())
    for p in sorted(set(changed_paths(root))):
        if fp_skip(p):
            continue
        h.update(b"\0" + p.encode())
        full = os.path.join(root, p)
        if os.path.isfile(full):
            with open(full, "rb") as f:
                h.update(hashlib.sha256(f.read()).digest())
        else:
            h.update(b"ABSENT")
    return h.hexdigest()


def staged_patch_hash(root, paths):
    code, out, _ = run(["git", "diff", "--cached", "--"] + list(paths), root)
    if code != 0:
        raise RuntimeError("git diff --cached failed")
    return hashlib.sha256(out.encode()).hexdigest()


def verify_freeze(state, root):
    fr = state.get("freeze")
    if not fr:
        return False, "no freeze recorded (run: freeze after git add of acceptance tests)"
    paths = fr.get("paths", [])
    if staged_patch_hash(root, paths) != fr.get("patch_hash"):
        return False, "staged acceptance-test patch is not byte-identical to the frozen patch"
    code, out, _ = run(["git", "diff", "--name-only", "--"] + paths, root)
    dirty = [l for l in out.splitlines() if l.strip()]
    if dirty:
        return False, "working-tree modification of frozen tests: " + ", ".join(dirty)
    return True, "frozen acceptance-test patch intact"


def exempt_active(state):
    return state.get("exempt_until", 0) > time.time()


def log_line(path, text):
    os.makedirs(BASE_DIR, exist_ok=True)
    with open(path, "a") as f:
        f.write(text.rstrip() + "\n")


def fail(msg):
    print("red-proof: FAIL: " + msg)
    sys.exit(1)


def ok(msg):
    print("red-proof: " + msg)


def parse_opts(argv):
    opts, rest, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            rest = argv[i + 1:]
            break
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[key] = argv[i + 1]
                i += 2
            else:
                opts[key] = True
                i += 1
        else:
            i += 1
    return opts, rest


def require_repo():
    root = repo_root(os.getcwd())
    if not root:
        fail("not inside a git repository")
    return root


def cmd_contract(argv):
    opts, _ = parse_opts(argv)
    path = opts.get("file")
    if not path or not os.path.isfile(path):
        fail("usage: contract --file <contract.md>")
    root = require_repo()
    with open(path, "rb") as f:
        text = f.read()
    chash = hashlib.sha256(text).hexdigest()
    os.makedirs(STATE_DIR, exist_ok=True)
    copy = state_path(root).replace(".json", ".contract.md")
    with open(copy, "wb") as f:
        f.write(text)
    state = {
        "phase": "CONTRACT_CREATED",
        "contract_hash": chash,
        "contract_copy": copy,
        "created": time.time(),
        "red_proofs": [],
        "evidence": {},
    }
    save_state(root, state)
    ok("contract registered (%s), phase=CONTRACT_CREATED, previous cycle state replaced" % chash[:12])


def cmd_red(argv):
    opts, cmd = parse_opts(argv)
    test = opts.get("test")
    red_type = opts.get("type")
    expected = opts.get("expected")
    if not (test and red_type in ("contract", "behavior") and expected and cmd):
        fail("usage: red --test <name> --type contract|behavior --expected '<reason>' -- <test command>")
    root = require_repo()
    state = load_state(root)
    if state.get("phase") not in ("CONTRACT_CREATED", "RED_CONFIRMED"):
        fail("red requires phase CONTRACT_CREATED (current: %s)" % state.get("phase"))
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if r.returncode == 0:
        fail("expected red, but the test command exited 0: not a valid red")
    tail = (r.stdout + "\n" + r.stderr)[-2000:]
    state.setdefault("red_proofs", []).append({
        "test": test,
        "red_type": red_type,
        "expected_failure": expected,
        "actual_output_tail": tail,
        "command": " ".join(cmd),
        "exit_code": r.returncode,
        "ts": time.time(),
    })
    state["phase"] = "RED_CONFIRMED"
    save_state(root, state)
    ok("red confirmed for %s (exit %d). Verify the actual failure reason matches: %s"
       % (test, r.returncode, expected))


def cmd_freeze(argv):
    root = require_repo()
    state = load_state(root)
    if state.get("phase") != "RED_CONFIRMED":
        fail("freeze requires phase RED_CONFIRMED (current: %s)" % state.get("phase"))
    code, out, _ = run(["git", "diff", "--cached", "--name-only"], root)
    paths = [l for l in out.splitlines() if l.strip()]
    if code != 0 or not paths:
        fail("nothing staged: git add the acceptance tests first")
    state["freeze"] = {
        "paths": paths,
        "patch_hash": staged_patch_hash(root, paths),
        "head": run(["git", "rev-parse", "HEAD"], root)[1].strip(),
        "contract_hash": state.get("contract_hash"),
        "ts": time.time(),
    }
    state["phase"] = "TESTS_FROZEN"
    save_state(root, state)
    ok("acceptance tests frozen (%d files), phase=TESTS_FROZEN, implementation may begin" % len(paths))


def cmd_check(argv):
    if not argv:
        fail("usage: check freeze|targeted|full-suite [-- <command>]")
    name = argv[0]
    _, cmd = parse_opts(argv[1:])
    root = require_repo()
    state = load_state(root)
    if name == "freeze":
        good, msg = verify_freeze(state, root)
        if not good:
            fail(msg)
        ok(msg)
        return
    if name not in ("targeted", "full-suite"):
        fail("unknown check: " + name)
    if state.get("phase") not in ("TESTS_FROZEN", "COMMIT_ISSUED"):
        fail("check %s requires phase TESTS_FROZEN (current: %s)" % (name, state.get("phase")))
    if not cmd:
        fail("usage: check %s -- <test command>" % name)
    r = subprocess.run(cmd, cwd=root)
    if r.returncode != 0:
        fail("%s run exited %d: evidence NOT recorded" % (name, r.returncode))
    state.setdefault("evidence", {})[name.replace("-", "_")] = {
        "fingerprint": fingerprint(root),
        "command": " ".join(cmd),
        "ts": time.time(),
    }
    save_state(root, state)
    ok("%s green, evidence bound to current code fingerprint" % name)


def cmd_attest(argv):
    opts, _ = parse_opts(argv)
    if not (opts.get("diff_reviewed") and opts.get("contract_ok")):
        fail("usage: attest --diff-reviewed --contract-ok (only after actually reading every changed hunk and checking each acceptance criterion)")
    root = require_repo()
    state = load_state(root)
    if state.get("phase") not in ("TESTS_FROZEN", "COMMIT_ISSUED"):
        fail("attest requires phase TESTS_FROZEN (current: %s)" % state.get("phase"))
    state.setdefault("evidence", {})["attest"] = {
        "fingerprint": fingerprint(root),
        "diff_reviewed": True,
        "contract_ok": True,
        "ts": time.time(),
    }
    save_state(root, state)
    ok("attestation recorded, bound to current code fingerprint")


def cmd_commit_gate(argv):
    root = require_repo()
    state = load_state(root)
    if state.get("phase") not in ("TESTS_FROZEN", "COMMIT_ISSUED"):
        fail("commit-gate requires phase TESTS_FROZEN (current: %s)" % state.get("phase"))
    problems = []
    good, msg = verify_freeze(state, root)
    if not good:
        problems.append("freeze: " + msg)
    if not state.get("red_proofs"):
        problems.append("no red proof recorded")
    fp = fingerprint(root)
    ev = state.get("evidence", {})
    for key in ("targeted", "full_suite", "attest"):
        item = ev.get(key)
        if not item:
            problems.append("missing evidence: " + key)
        elif item.get("fingerprint") != fp:
            problems.append("stale evidence (code changed since): " + key)
    if problems:
        fail("commit gate NOT passed:\n  - " + "\n  - ".join(problems))
    ev["commit_ready"] = {"fingerprint": fp, "ts": time.time()}
    state["evidence"] = ev
    save_state(root, state)
    ok("COMMIT GATE PASSED, exactly one git commit is now allowed for this code state")


def cmd_exempt(argv):
    opts, _ = parse_opts(argv)
    reason = opts.get("reason")
    if not reason or reason is True:
        fail("usage: exempt --reason '<why this task is exempt>' [--hours N]")
    hours = float(opts.get("hours", DEFAULT_EXEMPT_HOURS))
    root = require_repo()
    state = load_state(root)
    state["exempt_until"] = time.time() + hours * 3600
    state["exempt_reason"] = reason
    save_state(root, state)
    log_line(EXEMPT_LOG, "%s  %s  %.1fh  %s"
             % (time.strftime("%Y-%m-%d %H:%M:%S"), root, hours, reason))
    ok("exemption recorded for %.1fh: %s (logged to %s)" % (hours, reason, EXEMPT_LOG))


def cmd_status(argv):
    root = require_repo()
    state = load_state(root)
    good, msg = (verify_freeze(state, root) if state.get("freeze") else (None, "no freeze"))
    out = {
        "repo": root,
        "phase": state.get("phase"),
        "contract_hash": state.get("contract_hash"),
        "red_proofs": [r.get("test") for r in state.get("red_proofs", [])],
        "frozen_paths": state.get("freeze", {}).get("paths"),
        "freeze_check": msg,
        "evidence": {k: {"fingerprint": v.get("fingerprint", "")[:12], "ts": v.get("ts")}
                     for k, v in state.get("evidence", {}).items()},
        "current_fingerprint": fingerprint(root)[:12],
        "exempt_until": state.get("exempt_until"),
        "exempt_reason": state.get("exempt_reason"),
    }
    print(json.dumps(out, indent=2))


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


CLI = "python3 " + os.path.join(BASE_DIR, "red_proof.py")


def hook_edit(data):
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path")
    if not path:
        return
    path = os.path.realpath(os.path.expanduser(path))
    if any(path.startswith(p) for p in ALLOW_PATH_PREFIXES):
        return
    root = repo_root(os.path.dirname(path))
    if not root:
        return
    rel = "/" + os.path.relpath(path, root).replace(os.sep, "/")
    base = os.path.basename(path)
    if any(m in rel for m in NONPROD_MARKERS):
        return
    if base.startswith(NONPROD_PREFIXES) or rel.endswith(NONPROD_SUFFIXES):
        return
    state = load_state(root)
    if exempt_active(state):
        return
    phase = state.get("phase")
    if phase == "TESTS_FROZEN":
        return
    if phase in ("CONTRACT_CREATED", "RED_CONFIRMED"):
        deny("red-proof: acceptance tests are not frozen yet (phase %s). "
             "Complete the red phase (%s red ... -- <cmd>), then git add the "
             "acceptance tests and run: %s freeze. Production code may only "
             "change in phase TESTS_FROZEN." % (phase, CLI, CLI))
    if phase == "COMMIT_ISSUED":
        deny("red-proof: the previous commit cycle is closed. Start a new "
             "Commit Contract before further production changes: "
             "%s contract --file <contract.md>" % CLI)
    deny("red-proof: no active cycle for this repository. Production-code "
         "changes require the red-proof cycle (load the fable-context-maxxing skill). "
         "Start with: %s contract --file <contract.md>. For an exempt task "
         "(research, docs-only, trivial typo), classify it explicitly: "
         "%s exempt --reason '<why>'" % (CLI, CLI))


def strip_quoted(cmd):
    cmd = re.sub(r"'[^']*'", "''", cmd)
    cmd = re.sub(r'"[^"]*"', '""', cmd)
    return cmd


def bash_target_root(command, cwd):
    # A "git -C <dir>" hint counts only when that same invocation is the
    # commit: a -C on another subcommand does not move the commit. Apart from
    # that, only a leading "cd <dir>" counts. Subshells, variables, chained cd
    # and pushd stay out of scope and keep the reported cwd, as does any hint
    # that is not a git repository.
    word = r"'[^']*'|\"[^\"]*\"|[^\s;&|<>]+"
    sp = r"(?:[ \t]|\\\n)+"
    opt = r"-[cC]" + sp + r"(?:" + word + r")|--?[A-Za-z][-\w]*(?:=\S+)?"
    opts = r"(?:" + sp + r"(?:" + opt + r"))*"
    for m in (re.search(r"\bgit" + opts + sp + r"-C" + sp + r"(" + word +
                        r")" + opts + sp + r"commit\b", command),
              re.search(r"^\s*cd\s+(" + word + r")", command)):
        if not m:
            continue
        d = m.group(1)
        if len(d) > 1 and d[0] == d[-1] and d[0] in "'\"":
            d = d[1:-1]
        d = os.path.expanduser(d)
        if not os.path.isabs(d):
            d = os.path.join(cwd, d)
        if not os.path.isdir(d):
            continue
        root = repo_root(d)
        if root:
            return root
    return repo_root(cwd)


def hook_bash(data):
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not GIT_COMMIT_RE.search(strip_quoted(command)):
        return
    root = bash_target_root(command, data.get("cwd") or os.getcwd())
    if not root:
        return
    state = load_state(root)
    if exempt_active(state):
        log_line(EXEMPT_LOG, "%s  %s  commit under exemption: %s"
                 % (time.strftime("%Y-%m-%d %H:%M:%S"), root,
                    state.get("exempt_reason")))
        return
    ev = state.get("evidence", {})
    ready = ev.get("commit_ready")
    if ready and ready.get("fingerprint") == fingerprint(root):
        good, msg = verify_freeze(state, root)
        if good:
            del ev["commit_ready"]
            state["evidence"] = ev
            state["phase"] = "COMMIT_ISSUED"
            save_state(root, state)
            return
        deny("red-proof: freeze violated at commit time: " + msg)
    if ready:
        deny("red-proof: Commit Gate evidence is stale, code changed since "
             "verification. Re-run checks and %s commit-gate." % CLI)
    deny("red-proof: Commit Gate has not passed for this repository. "
         "Required: %s check targeted -- <cmd>; check full-suite -- <cmd>; "
         "attest --diff-reviewed --contract-ok; commit-gate. For an exempt "
         "task: %s exempt --reason '<why>'" % (CLI, CLI))


def main():
    argv = sys.argv[1:]
    if not argv:
        fail("usage: contract|red|freeze|check|attest|commit-gate|exempt|status|hook")
    cmd = argv[0]
    if cmd == "hook":
        try:
            data = json.load(sys.stdin)
            if len(argv) > 1 and argv[1] == "edit":
                hook_edit(data)
            elif len(argv) > 1 and argv[1] == "bash":
                hook_bash(data)
        except SystemExit:
            raise
        except Exception as e:
            try:
                log_line(ERROR_LOG, "%s  hook error: %r"
                         % (time.strftime("%Y-%m-%d %H:%M:%S"), e))
            except OSError:
                pass
        sys.exit(0)
    handlers = {
        "contract": cmd_contract,
        "red": cmd_red,
        "freeze": cmd_freeze,
        "check": cmd_check,
        "attest": cmd_attest,
        "commit-gate": cmd_commit_gate,
        "exempt": cmd_exempt,
        "status": cmd_status,
    }
    fn = handlers.get(cmd)
    if not fn:
        fail("unknown command: " + cmd)
    fn(argv[1:])


if __name__ == "__main__":
    main()
